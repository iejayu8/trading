"""
optimize.py – Grid-search parameter optimizer for the trading strategy.

Iterates over combinations of key strategy parameters, runs the backtester
on each, and ranks results by a composite score that rewards return while
penalizing drawdown and requiring a minimum number of trades.

Performance note: indicators are pre-computed once on the full dataset.
The simulation loop uses pre-computed arrays to avoid redundant pandas work.

Usage
─────
    cd backtest
    python optimize.py               # full grid, current data
    python optimize.py --top 20      # show top 20 results
    python optimize.py --min-trades 15  # require at least 15 trades
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Allow importing backend modules
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
import strategy
from strategy import Signal, compute_indicators
from fetch_data import load_or_fetch

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

SLIPPAGE = 0.0005   # 0.05 % per side (matches Backtest class)
FEE_RATE  = 0.0006  # 0.06 % taker fee


# ── Parameter grid ────────────────────────────────────────────────────────────
PARAM_GRID: dict[str, list] = {
    # Entry signal parameters (in strategy.py)
    # ADX: lower = more trades but noisier; higher = fewer, cleaner
    "ADX_MIN":             [18.0, 20.0, 22.0, 25.0],
    # RSI pullback depth: how oversold must the dip be
    "RSI_PULLBACK_MAX":    [38.0, 42.0, 46.0],
    # RSI recovery: how much must RSI recover before entry
    "RSI_RECOVERY_LONG":   [49.0, 52.0, 55.0],
    # Lookback: freshness of the pullback signal
    "PULLBACK_LOOKBACK":   [3, 4, 6],
    # Cooldown: bars between signals (24=6h, 36=9h, 48=12h)
    "SIGNAL_COOLDOWN":     [24, 36, 48],
    # Risk management parameters (in config.py)
    "STOP_LOSS_PCT":       [0.015, 0.020, 0.025],
    "TAKE_PROFIT_PCT":     [0.040, 0.055, 0.070],
}

# Baseline (current v6) values – used for display only
BASELINE: dict[str, float] = {
    "ADX_MIN":           22.0,
    "RSI_PULLBACK_MAX":  42.0,
    "RSI_RECOVERY_LONG": 52.0,
    "PULLBACK_LOOKBACK": 4,
    "SIGNAL_COOLDOWN":   48,
    "STOP_LOSS_PCT":     0.020,
    "TAKE_PROFIT_PCT":   0.045,
}


# ── Fast inline backtester ────────────────────────────────────────────────────

def _run_fast(
    arrays: dict,           # pre-extracted numpy arrays
    timestamps: list[str],  # ISO timestamps per bar
    n: int,                 # total bar count
    adx_min: float,
    rsi_pb_max: float,      # RSI pullback threshold (LONG)
    rsi_rc_long: float,     # RSI recovery threshold (LONG)
    rsi_pb_min: float,      # RSI spike threshold (SHORT) = 100 - rsi_pb_max
    rsi_rc_short: float,    # RSI rejection threshold (SHORT) = 100 - rsi_rc_long
    lookback: int,
    cooldown: int,
    sl_pct: float,
    tp_pct: float,
    min_trades: int,
    initial_equity: float = 1000.0,
    max_daily_loss_pct: float = 0.03,
) -> dict | None:
    """
    Fast inline simulation loop.  No pandas per-combo overhead.
    Returns summary dict or None if trade count < min_trades.
    """
    close  = arrays["close"]
    high   = arrays["high"]
    low    = arrays["low"]
    open_  = arrays["open"]
    rsi    = arrays["rsi"]
    adx    = arrays["adx"]
    ema_fast  = arrays["ema_fast"]
    ema_slow  = arrays["ema_slow"]
    ema_trend = arrays["ema_trend"]
    ema_200   = arrays["ema_200"]
    macd_hist = arrays["macd_hist"]
    volume    = arrays["volume"]
    vol_sma   = arrays["volume_sma"]
    ema_slow_gt_trend = ema_slow > ema_trend  # pre-computed boolean array

    equity = initial_equity
    trades: list[dict] = []
    open_trade: dict | None = None
    last_signal_bar = -cooldown
    daily_pnl: dict[str, float] = {}

    start = config.TREND_EMA + 5

    for i in range(start, n):
        ts_today = timestamps[i][:10]

        # ── Manage open trade ─────────────────────────────────────────────
        if open_trade is not None:
            sl = open_trade["sl"]
            tp = open_trade["tp"]
            h = high[i]
            lo = low[i]
            direction = open_trade["direction"]

            hit_sl = (direction == "LONG" and lo <= sl) or (direction == "SHORT" and h >= sl)
            hit_tp = (direction == "LONG" and h >= tp) or (direction == "SHORT" and lo <= tp)

            if hit_sl or hit_tp:
                exit_price = sl if hit_sl else tp
                if direction == "LONG":
                    exit_price *= (1 - SLIPPAGE)
                else:
                    exit_price *= (1 + SLIPPAGE)

                entry = open_trade["entry_price"]
                size  = open_trade["size"]
                pct   = (exit_price - entry) / entry if direction == "LONG" else (entry - exit_price) / entry
                pnl   = pct * entry * size
                fee_c = exit_price * size * FEE_RATE
                net   = pnl - fee_c - open_trade["fee_open"]

                equity += net
                reason = "SL" if hit_sl else "TP"
                trade_record = {"pnl": net, "reason": reason, "closed_at": timestamps[i]}
                trades.append(trade_record)

                day = timestamps[i][:10]
                daily_pnl[day] = daily_pnl.get(day, 0.0) + net
                open_trade = None

        # ── Daily loss guard ─────────────────────────────────────────────
        if equity > 0 and daily_pnl.get(ts_today, 0.0) / equity < -max_daily_loss_pct:
            continue

        if open_trade is not None:
            continue  # already in trade

        # ── Signal generation ─────────────────────────────────────────────
        if i - last_signal_bar < cooldown:
            continue
        if adx[i] < adx_min:
            continue
        if np.isnan(adx[i]) or np.isnan(rsi[i]):
            continue

        # RSI window for pullback check (avoid index errors)
        pb_start = max(0, i - lookback)
        rsi_window = rsi[pb_start:i]  # bars before current

        # --- LONG ---
        price_above_ema200 = close[i] > ema_200[i]
        ema21_above_ema55  = ema_slow_gt_trend[i]
        recent_pb_long     = np.any(rsi_window <= rsi_pb_max)
        rsi_recovered      = rsi[i] >= rsi_rc_long
        price_above_ema9   = close[i] > ema_fast[i]
        macd_ok_long       = macd_hist[i] >= -50
        vol_ok             = (not np.isnan(vol_sma[i])) and volume[i] >= 0.9 * vol_sma[i]

        if (price_above_ema200 and ema21_above_ema55 and recent_pb_long
                and rsi_recovered and price_above_ema9 and macd_ok_long and vol_ok):
            signal = "LONG"
        else:
            # --- SHORT ---
            price_below_ema200 = close[i] < ema_200[i]
            ema21_below_ema55  = not ema21_above_ema55
            recent_pb_short    = np.any(rsi_window >= rsi_pb_min)
            rsi_rejected       = rsi[i] <= rsi_rc_short
            price_below_ema9   = close[i] < ema_fast[i]
            macd_ok_short      = macd_hist[i] <= 50

            if (price_below_ema200 and ema21_below_ema55 and recent_pb_short
                    and rsi_rejected and price_below_ema9 and macd_ok_short and vol_ok):
                signal = "SHORT"
            else:
                continue

        # ── Enter at next bar's open ──────────────────────────────────────
        ni = i + 1
        if ni >= n:
            break

        entry_price = float(open_[ni])
        if signal == "LONG":
            entry_price *= (1 + SLIPPAGE)
        else:
            entry_price *= (1 - SLIPPAGE)

        # Position size: risk_amount / (entry × sl_pct)
        risk_amount = equity * config.RISK_PER_TRADE
        size = risk_amount / (entry_price * sl_pct)
        size = max(round(size, 4), 0.001)

        sl_price = entry_price * (1 - sl_pct) if signal == "LONG" else entry_price * (1 + sl_pct)
        tp_price = entry_price * (1 + tp_pct) if signal == "LONG" else entry_price * (1 - tp_pct)

        fee_open = entry_price * size * FEE_RATE
        equity -= fee_open

        open_trade = {
            "direction":   signal,
            "entry_price": entry_price,
            "size":        size,
            "sl":          sl_price,
            "tp":          tp_price,
            "fee_open":    fee_open,
        }
        last_signal_bar = i

    # Force-close any remaining trade
    if open_trade is not None:
        ep = float(close[-1])
        direction = open_trade["direction"]
        ep *= (1 - SLIPPAGE) if direction == "LONG" else (1 + SLIPPAGE)
        entry = open_trade["entry_price"]
        size  = open_trade["size"]
        pct   = (ep - entry) / entry if direction == "LONG" else (entry - ep) / entry
        pnl   = pct * entry * size
        fee_c = ep * size * FEE_RATE
        net   = pnl - fee_c - open_trade["fee_open"]
        equity += net
        trades.append({"pnl": net, "reason": "EOD", "closed_at": timestamps[-1]})

    if len(trades) < min_trades:
        return None

    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = len(wins) / len(pnls) * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")

    # Max drawdown
    curve = np.concatenate([[initial_equity], initial_equity + np.cumsum(pnls)])
    peak = np.maximum.accumulate(curve)
    dd = ((curve - peak) / peak).min() * 100

    return_pct = (equity - initial_equity) / initial_equity * 100

    return {
        "return_pct":       round(return_pct, 2),
        "win_rate_pct":     round(win_rate, 2),
        "profit_factor":    round(float(pf), 3),
        "max_drawdown_pct": round(float(dd), 2),
        "total_trades":     len(trades),
    }


def _score(r: dict) -> float:
    """Calmar-like ratio: return per unit of drawdown."""
    dd = abs(r["max_drawdown_pct"]) or 1.0
    return r["return_pct"] / dd


def run_grid(df: pd.DataFrame, min_trades: int = 15) -> list[dict]:
    """Pre-compute indicators once, then grid-search all parameter combos."""
    print("Pre-computing indicators … ", end="", flush=True)
    df_ind = compute_indicators(df.copy())
    print("done.\n")

    # Extract numpy arrays (fast access in inner loop)
    arrays = {
        "close":      df_ind["close"].to_numpy(float),
        "high":       df_ind["high"].to_numpy(float),
        "low":        df_ind["low"].to_numpy(float),
        "open":       df_ind["open"].to_numpy(float),
        "rsi":        df_ind["rsi"].to_numpy(float),
        "adx":        df_ind["adx"].to_numpy(float),
        "ema_fast":   df_ind["ema_fast"].to_numpy(float),
        "ema_slow":   df_ind["ema_slow"].to_numpy(float),
        "ema_trend":  df_ind["ema_trend"].to_numpy(float),
        "ema_200":    df_ind["ema_200"].to_numpy(float),
        "macd_hist":  df_ind["macd_hist"].to_numpy(float),
        "volume":     df_ind["volume"].to_numpy(float),
        "volume_sma": df_ind["volume_sma"].to_numpy(float),
    }
    timestamps = [str(ts) for ts in df_ind.index]
    n = len(df_ind)

    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))
    total = len(combos)

    print(f"Running {total} parameter combinations …\n")

    results = []
    for idx, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))

        adx_min      = params["ADX_MIN"]
        rsi_pb_max   = params["RSI_PULLBACK_MAX"]
        rsi_rc_long  = params["RSI_RECOVERY_LONG"]
        rsi_pb_min   = 100.0 - rsi_pb_max   # mirror for SHORT
        rsi_rc_short = 100.0 - rsi_rc_long  # mirror for SHORT
        lookback     = params["PULLBACK_LOOKBACK"]
        cooldown     = params["SIGNAL_COOLDOWN"]
        sl_pct       = params["STOP_LOSS_PCT"]
        tp_pct       = params["TAKE_PROFIT_PCT"]

        r = _run_fast(
            arrays=arrays,
            timestamps=timestamps,
            n=n,
            adx_min=adx_min,
            rsi_pb_max=rsi_pb_max,
            rsi_rc_long=rsi_rc_long,
            rsi_pb_min=rsi_pb_min,
            rsi_rc_short=rsi_rc_short,
            lookback=lookback,
            cooldown=cooldown,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            min_trades=min_trades,
        )

        if r is not None:
            entry = {**params, **r, "score": _score(r)}
            results.append(entry)

        if idx % 500 == 0 or idx == total:
            pct = idx / total * 100
            best = max(results, key=lambda x: x["score"])["return_pct"] if results else 0
            print(f"  [{idx:>5}/{total}] {pct:.0f}%  best return so far: {best:+.2f}%")

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def print_results_table(results: list[dict], top_n: int = 20) -> None:
    header = (
        f"{'#':>3}  "
        f"{'ADX':>5}  "
        f"{'RSI_PB':>6}  "
        f"{'RSI_RC':>6}  "
        f"{'LB':>2}  "
        f"{'CD':>3}  "
        f"{'SL%':>5}  "
        f"{'TP%':>5}  "
        f"{'Return':>8}  "
        f"{'WR%':>5}  "
        f"{'PF':>5}  "
        f"{'DD%':>7}  "
        f"{'Trades':>6}  "
        f"{'Score':>7}"
    )
    print("=" * len(header))
    print("  TOP PARAMETER COMBINATIONS")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for i, r in enumerate(results[:top_n], 1):
        row = (
            f"{i:>3}  "
            f"{r['ADX_MIN']:>5.1f}  "
            f"{r['RSI_PULLBACK_MAX']:>6.1f}  "
            f"{r['RSI_RECOVERY_LONG']:>6.1f}  "
            f"{r['PULLBACK_LOOKBACK']:>2}  "
            f"{r['SIGNAL_COOLDOWN']:>3}  "
            f"{r['STOP_LOSS_PCT']*100:>5.2f}  "
            f"{r['TAKE_PROFIT_PCT']*100:>5.2f}  "
            f"{r['return_pct']:>+8.2f}%  "
            f"{r['win_rate_pct']:>5.1f}%  "
            f"{r['profit_factor']:>5.3f}  "
            f"{r['max_drawdown_pct']:>7.2f}%  "
            f"{r['total_trades']:>6}  "
            f"{r['score']:>7.3f}"
        )
        print(row)

    print("=" * len(header))
    print(f"\n  BASELINE (v6):  ADX={BASELINE['ADX_MIN']:.1f}  "
          f"RSI_PB={BASELINE['RSI_PULLBACK_MAX']:.1f}  "
          f"RSI_RC={BASELINE['RSI_RECOVERY_LONG']:.1f}  "
          f"LB={BASELINE['PULLBACK_LOOKBACK']}  "
          f"CD={BASELINE['SIGNAL_COOLDOWN']}  "
          f"SL={BASELINE['STOP_LOSS_PCT']*100:.2f}%  "
          f"TP={BASELINE['TAKE_PROFIT_PCT']*100:.2f}%\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy parameter optimizer")
    parser.add_argument("--top",        type=int, default=20, help="Show top N results")
    parser.add_argument("--min-trades", type=int, default=15, help="Minimum trade count")
    parser.add_argument("--fresh",      action="store_true",  help="Re-fetch candle data")
    parser.add_argument("--days",       type=int, default=365, help="Days of history")
    args = parser.parse_args()

    if args.fresh:
        csv = Path(__file__).parent / "data" / "BTCUSDT_15m.csv"
        csv.unlink(missing_ok=True)

    df = load_or_fetch("BTCUSDT", days=args.days)
    print(f"Loaded {len(df)} candles  ({df.index[0]} → {df.index[-1]})\n")

    results = run_grid(df, min_trades=args.min_trades)

    if not results:
        print("No valid results found. Try lowering --min-trades.")
        return

    print_results_table(results, top_n=args.top)

    # Save full results
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"optimize_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Full results saved to {out_path}\n")

    # Print best params as ready-to-apply code
    best = results[0]
    print("  Best parameters (copy into strategy.py / config.py):")
    print(f"    ADX_MIN           = {best['ADX_MIN']}")
    print(f"    RSI_PULLBACK_MAX  = {best['RSI_PULLBACK_MAX']}")
    print(f"    RSI_RECOVERY_LONG = {best['RSI_RECOVERY_LONG']}")
    print(f"    PULLBACK_LOOKBACK = {best['PULLBACK_LOOKBACK']}")
    print(f"    SIGNAL_COOLDOWN   = {best['SIGNAL_COOLDOWN']}")
    print(f"    STOP_LOSS_PCT     = {best['STOP_LOSS_PCT']}")
    print(f"    TAKE_PROFIT_PCT   = {best['TAKE_PROFIT_PCT']}")
    print()


if __name__ == "__main__":
    main()

