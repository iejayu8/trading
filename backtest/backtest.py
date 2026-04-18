"""
backtest.py – Strategy backtester.

Simulates the EMA + RSI + Volume strategy on historical BTC/USDT
15-minute data and outputs a detailed performance report.

Usage
─────
    cd backtest
    python backtest.py               # uses cached CSV or fetches from BloFin
    python backtest.py --fresh       # force re-fetch data
    python backtest.py --equity 1000 # start with custom equity
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np

# Allow importing from parent/backend
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
from strategy import (
    Signal,
    calculate_position_size,
    calculate_sl_tp,
    compute_indicators,
    generate_signal,
    reset_signal_state,
)
from fetch_data import load_or_fetch

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ── Backtester ────────────────────────────────────────────────────────────────

class Backtest:
    """
    Event-driven backtester that replays each 15-min candle.

    Position model
    ──────────────
    • At most one open position per symbol at a time.
    • Entry on candle close of signal candle (next open is used as fill price).
    • SL/TP checked against the subsequent candle's high/low.
    • Slippage: 0.05 % per side.
    • Fees: 0.06 % per side (taker fee on BloFin).
    """

    SLIPPAGE = 0.0005  # 0.05 %
    FEE_RATE = 0.0006  # 0.06 % taker fee

    def __init__(self, initial_equity: float = 1000.0, symbol: str = "BTC-USDT") -> None:
        self.initial_equity = initial_equity
        self.symbol = symbol
        self.equity = initial_equity
        self.trades: list[dict] = []
        self._open: dict | None = None  # current open trade

    def run(self, df: pd.DataFrame) -> dict:
        """Run the backtest on a complete OHLCV DataFrame."""
        df = compute_indicators(df)
        reset_signal_state(self.symbol)  # ensure each run starts with a clean cooldown slate
        params = config.get_symbol_params(self.symbol)

        for i in range(config.TREND_EMA + 5, len(df)):
            candle = df.iloc[i]
            prev_window = df.iloc[: i + 1]

            # ── Manage open trade ────────────────────────────────────────────
            if self._open:
                closed = self._check_exit(self._open, candle)
                if closed:
                    self._open = None

            # ── Daily loss guard ─────────────────────────────────────────────
            if self._daily_loss_exceeded(candle.name):
                continue

            # ── Generate signal ───────────────────────────────────────────────
            if self._open:
                continue  # already in a trade

            signal = generate_signal(prev_window, symbol=self.symbol)
            if signal == Signal.NONE:
                continue

            # Entry at next candle's open (use current candle open as proxy
            # since we evaluate at close and enter on next open)
            next_idx = i + 1
            if next_idx >= len(df):
                break
            next_candle = df.iloc[next_idx]
            entry_price = float(next_candle["open"])

            # Apply slippage
            if signal == Signal.LONG:
                entry_price *= 1 + self.SLIPPAGE
            else:
                entry_price *= 1 - self.SLIPPAGE

            size = calculate_position_size(
                self.equity, entry_price,
                stop_loss_pct=params["stop_loss_pct"],
            )
            sl, tp = calculate_sl_tp(
                entry_price, signal,
                stop_loss_pct=params["stop_loss_pct"],
                take_profit_pct=params["take_profit_pct"],
            )

            # Fee on open
            fee = entry_price * size * self.FEE_RATE
            self.equity -= fee

            self._open = {
                "direction": signal,
                "entry_price": entry_price,
                "size": size,
                "sl": sl,
                "tp": tp,
                "opened_at": str(candle.name),
                "fee_open": fee,
            }

        # Close any open trade at last price
        if self._open:
            last = df.iloc[-1]
            self._force_close(self._open, float(last["close"]), str(last.name))
            self._open = None

        return self._summary()

    # ── Exit logic ────────────────────────────────────────────────────────────

    def _check_exit(self, trade: dict, candle) -> bool:
        """Check if SL or TP is hit on this candle. Return True if closed."""
        direction = trade["direction"]
        sl = trade["sl"]
        tp = trade["tp"]
        high = float(candle["high"])
        low = float(candle["low"])

        hit_sl = (direction == Signal.LONG and low <= sl) or (
            direction == Signal.SHORT and high >= sl
        )
        hit_tp = (direction == Signal.LONG and high >= tp) or (
            direction == Signal.SHORT and low <= tp
        )

        if not hit_sl and not hit_tp:
            return False

        # Assume worst-case fill: SL if both hit
        exit_price = sl if hit_sl else tp
        if direction == Signal.LONG:
            exit_price *= 1 - self.SLIPPAGE
        else:
            exit_price *= 1 + self.SLIPPAGE

        self._close_trade(trade, exit_price, "SL" if hit_sl else "TP", str(candle.name))
        return True

    def _force_close(self, trade: dict, price: float, ts: str) -> None:
        if trade["direction"] == Signal.LONG:
            price *= 1 - self.SLIPPAGE
        else:
            price *= 1 + self.SLIPPAGE
        self._close_trade(trade, price, "EOD", ts)

    def _close_trade(
        self, trade: dict, exit_price: float, reason: str, closed_at: str
    ) -> None:
        direction = trade["direction"]
        entry = trade["entry_price"]
        size = trade["size"]

        if direction == Signal.LONG:
            pct = (exit_price - entry) / entry
        else:
            pct = (entry - exit_price) / entry

        # P&L = pct × notional value (leverage is already encoded in size
        # via position sizing: size = equity×risk_pct / (entry×stop_pct))
        pnl = pct * entry * size
        fee_close = exit_price * size * self.FEE_RATE
        # fee_open was already deducted from equity at entry time,
        # so only subtract the closing fee here to avoid double-counting.
        net_pnl = pnl - fee_close

        self.equity += net_pnl

        self.trades.append(
            {
                "direction": direction,
                "entry_price": entry,
                "exit_price": exit_price,
                "size": size,
                "pnl": round(net_pnl, 4),
                "reason": reason,
                "opened_at": trade["opened_at"],
                "closed_at": closed_at,
            }
        )

    # ── Daily loss guard ──────────────────────────────────────────────────────

    def _daily_loss_exceeded(self, ts) -> bool:
        today = str(ts)[:10]
        daily_pnl = sum(
            t["pnl"] for t in self.trades if t["closed_at"][:10] == today
        )
        # Use initial_equity as denominator to match the bot's paper-mode
        # behaviour which uses PAPER_START_EQUITY (a fixed starting balance).
        if self.initial_equity > 0:
            return (daily_pnl / self.initial_equity) < -config.MAX_DAILY_LOSS_PCT
        return False

    # ── Summary ───────────────────────────────────────────────────────────────

    def _summary(self) -> dict:
        if not self.trades:
            return {"error": "No trades generated"}

        df_t = pd.DataFrame(self.trades)
        total = len(df_t)
        wins = (df_t["pnl"] > 0).sum()
        losses = total - wins
        win_rate = wins / total * 100

        total_pnl = df_t["pnl"].sum()
        avg_win = df_t.loc[df_t["pnl"] > 0, "pnl"].mean() if wins > 0 else 0
        avg_loss = df_t.loc[df_t["pnl"] <= 0, "pnl"].mean() if losses > 0 else 0
        profit_factor = (
            df_t.loc[df_t["pnl"] > 0, "pnl"].sum()
            / abs(df_t.loc[df_t["pnl"] <= 0, "pnl"].sum())
            if losses > 0 and df_t.loc[df_t["pnl"] <= 0, "pnl"].sum() != 0
            else float("inf")
        )

        # Equity curve for max drawdown
        equity_curve = [self.initial_equity]
        running = self.initial_equity
        for pnl in df_t["pnl"]:
            running += pnl
            equity_curve.append(running)
        equity_arr = np.array(equity_curve)
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (equity_arr - peak) / peak
        max_dd = float(drawdown.min() * 100)

        return_pct = (self.equity - self.initial_equity) / self.initial_equity * 100

        return {
            "initial_equity": self.initial_equity,
            "final_equity": round(self.equity, 4),
            "return_pct": round(return_pct, 2),
            "total_trades": total,
            "wins": int(wins),
            "losses": int(losses),
            "win_rate_pct": round(float(win_rate), 2),
            "profit_factor": round(float(profit_factor), 3),
            "total_pnl": round(float(total_pnl), 4),
            "avg_win": round(float(avg_win), 4),
            "avg_loss": round(float(avg_loss), 4),
            "max_drawdown_pct": round(max_dd, 2),
            "trades": df_t.to_dict(orient="records"),
        }


# ── CLI entry point ───────────────────────────────────────────────────────────

def _run_single(symbol: str, equity: float, days: int, fresh: bool) -> dict | None:
    """Run backtest for a single symbol.

    Parameters
    ----------
    symbol : str
        BloFin instrument ID (e.g. ``"BTC-USDT"``).
    equity : float
        Starting equity in USDT.
    days : int
        Number of days of historical data to use.
    fresh : bool
        When True, delete any cached CSV and re-fetch data.

    Returns the results dict or None on error.
    """
    if fresh:
        csv = Path(__file__).parent / "data" / f"{symbol}_15m.csv"
        csv.unlink(missing_ok=True)

    df = load_or_fetch(symbol, days=days)
    print(f"\nLoaded {len(df)} candles  ({df.index[0]} → {df.index[-1]})\n")

    bt = Backtest(initial_equity=equity, symbol=symbol)
    results = bt.run(df)

    if "error" in results:
        print(f"ERROR ({symbol}): {results['error']}")
        return None

    # Per-symbol params for display
    sym_params = config.get_symbol_params(symbol)

    # Pretty print summary
    print("=" * 55)
    print("  BACKTEST RESULTS")
    print("=" * 55)
    print(f"  Symbol          : {symbol}")
    print(f"  Timeframe       : 15m")
    print(f"  Leverage        : {config.LEVERAGE}x")
    print(f"  Risk/trade      : {config.RISK_PER_TRADE*100:.1f}%")
    print(f"  SL / TP         : {sym_params['stop_loss_pct']*100:.1f}% / {sym_params['take_profit_pct']*100:.1f}%")
    print("-" * 55)
    print(f"  Initial equity  : ${results['initial_equity']:.2f}")
    print(f"  Final equity    : ${results['final_equity']:.2f}")
    print(f"  Return          : {results['return_pct']:+.2f}%")
    print(f"  Total trades    : {results['total_trades']}")
    print(f"  Win rate        : {results['win_rate_pct']:.1f}%")
    print(f"  Profit factor   : {results['profit_factor']:.3f}")
    print(f"  Avg win         : ${results['avg_win']:.2f}")
    print(f"  Avg loss        : ${results['avg_loss']:.2f}")
    print(f"  Max drawdown    : {results['max_drawdown_pct']:.2f}%")
    print("=" * 55)

    # Save JSON results
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"backtest_{symbol}_{ts}.json"
    results_to_save = {k: v for k, v in results.items() if k != "trades"}
    results_to_save["symbol"] = symbol
    results_to_save["trades_count"] = len(results.get("trades", []))
    with open(out_path, "w") as f:
        json.dump(results_to_save, f, indent=2)
    print(f"\nResults saved to {out_path}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy backtester")
    parser.add_argument("--symbol", type=str, default="BTC-USDT", help="BloFin symbol (e.g. ETH-USDT)")
    parser.add_argument("--all", action="store_true", help="Run on all supported symbols")
    parser.add_argument("--fresh", action="store_true", help="Re-fetch data")
    parser.add_argument(
        "--equity", type=float, default=1000.0, help="Starting equity (USDT)"
    )
    parser.add_argument("--days", type=int, default=365, help="Days of history")
    args = parser.parse_args()

    if args.all:
        symbols = list(config.SUPPORTED_SYMBOLS)
    else:
        symbols = [args.symbol]

    all_results: list[dict] = []
    for sym in symbols:
        result = _run_single(sym, args.equity, args.days, args.fresh)
        if result is not None:
            result["symbol"] = sym
            all_results.append(result)

    if len(all_results) > 1:
        print("\n")
        print("=" * 65)
        print("  COMBINED SUMMARY (all symbols)")
        print("=" * 65)
        total_pnl = sum(r["total_pnl"] for r in all_results)
        total_trades = sum(r["total_trades"] for r in all_results)
        total_wins = sum(r["wins"] for r in all_results)
        avg_return = sum(r["return_pct"] for r in all_results) / len(all_results)
        overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

        for r in all_results:
            print(f"  {r['symbol']:>10s}  return={r['return_pct']:+6.2f}%  "
                  f"WR={r['win_rate_pct']:5.1f}%  trades={r['total_trades']:3d}  "
                  f"DD={r['max_drawdown_pct']:6.2f}%")
        print("-" * 65)
        print(f"  {'TOTAL':>10s}  avg_return={avg_return:+6.2f}%  "
              f"WR={overall_wr:5.1f}%  trades={total_trades:3d}  "
              f"PnL=${total_pnl:+.2f}")
        print("=" * 65)


if __name__ == "__main__":
    main()
