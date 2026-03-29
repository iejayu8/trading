"""
bot.py – Core trading bot engine.

The bot runs as a background thread, waking every 15 minutes
(aligned to the candle close) to evaluate signals and manage positions.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pandas as pd

import config
import database as db
from exchange import BloFinClient
from strategy import (
    Signal,
    calculate_position_size,
    calculate_sl_tp,
    compute_indicators,
    generate_signal,
)


class TradingBot:
    """15-minute candle trading bot for BloFin futures."""

    CANDLE_SECONDS = 15 * 60  # 900 s

    def __init__(self, symbol: str = None) -> None:
        self.symbol = symbol or config.TRADING_SYMBOL
        self.leverage = config.LEVERAGE
        self._client = BloFinClient()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        db.init_db()
        db.update_bot_status(running=0, symbol=self.symbol)
        db.log_event(f"Bot initialised for {self.symbol} (leverage {self.leverage}x)")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        db.update_bot_status(running=1)
        db.log_event("Bot started")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        db.update_bot_status(running=0)
        db.log_event("Bot stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        db.log_event("Trading loop started")
        while self._running:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                db.log_event(f"Error in tick: {exc}", level="ERROR")

            # Sleep until the next 15-min candle boundary (+ 5 s buffer)
            now = time.time()
            next_candle = (
                (now // self.CANDLE_SECONDS + 1) * self.CANDLE_SECONDS + 5
            )
            sleep_secs = max(next_candle - time.time(), 1)
            time.sleep(sleep_secs)

        db.log_event("Trading loop exited")

    def _tick(self) -> None:
        """Evaluate market and act on one candle close."""
        db.log_event(f"Tick – fetching candles for {self.symbol}")

        # ── Fetch OHLCV data ─────────────────────────────────────────────────
        raw = self._client.get_candles(self.symbol, bar=config.TIMEFRAME, limit=200)
        if not raw:
            db.log_event("No candle data received", level="WARNING")
            return

        df = _candles_to_df(raw)
        df = compute_indicators(df)

        last_price = float(df["close"].iloc[-1])
        db.update_bot_status(last_price=last_price)

        # ── Account equity ───────────────────────────────────────────────────
        try:
            balance_data = self._client.get_balance()
            equity = _extract_usdt_equity(balance_data)
        except Exception:
            equity = None

        if equity is not None:
            db.update_bot_status(equity=equity)

        # ── Daily loss guard ─────────────────────────────────────────────────
        if equity is not None and self._daily_loss_exceeded(equity):
            db.log_event("Daily loss limit reached – skipping signal", level="WARNING")
            return

        # ── Manage existing position ──────────────────────────────────────────
        open_trades = db.get_open_trades(symbol=self.symbol)
        self._manage_open_trades(open_trades, last_price)

        # ── Generate signal ───────────────────────────────────────────────────
        signal = generate_signal(df)
        db.update_bot_status(last_signal=signal)
        db.log_event(f"Signal: {signal} @ {last_price:.2f}")

        # ── Enter new position ────────────────────────────────────────────────
        if signal != Signal.NONE and not open_trades and equity:
            self._enter_trade(signal, last_price, equity)

    # ── Trade management ──────────────────────────────────────────────────────

    def _manage_open_trades(
        self, open_trades: list[dict], current_price: float
    ) -> None:
        for trade in open_trades:
            direction = trade["direction"]
            entry = trade["entry_price"]
            sl = trade["sl_price"]
            tp = trade["tp_price"]
            size = trade["size"]

            hit_sl = (
                (direction == Signal.LONG and current_price <= sl)
                or (direction == Signal.SHORT and current_price >= sl)
            )
            hit_tp = (
                (direction == Signal.LONG and current_price >= tp)
                or (direction == Signal.SHORT and current_price <= tp)
            )

            if hit_sl or hit_tp:
                pnl = _calc_pnl(direction, entry, current_price, size)
                db.close_trade(trade["id"], current_price, pnl)
                reason = "TP" if hit_tp else "SL"
                db.log_event(
                    f"Trade {trade['id']} closed ({reason}) "
                    f"@ {current_price:.2f}  PnL={pnl:.4f} USDT"
                )

                # Place close order on exchange (best-effort)
                try:
                    close_side = "sell" if direction == Signal.LONG else "buy"
                    self._client.place_order(
                        self.symbol, close_side, "market", size
                    )
                except Exception as exc:
                    db.log_event(f"Close order failed: {exc}", level="ERROR")

    def _enter_trade(
        self, signal: str, price: float, equity: float
    ) -> None:
        size = calculate_position_size(equity, price)
        sl, tp = calculate_sl_tp(price, signal)
        side = "buy" if signal == Signal.LONG else "sell"

        # Set leverage first
        try:
            self._client.set_leverage(self.symbol, self.leverage)
        except Exception as exc:
            db.log_event(f"Set leverage failed: {exc}", level="WARNING")

        # Record in DB first (so we track even if exchange call fails)
        trade_id = db.open_trade(
            self.symbol, signal, price, size, sl, tp, self.leverage
        )

        # Place order on exchange
        try:
            self._client.place_order(
                self.symbol, side, "market", size, sl_price=sl, tp_price=tp
            )
            db.log_event(
                f"Trade {trade_id} opened: {signal} {size} {self.symbol} "
                f"@ {price:.2f}  SL={sl}  TP={tp}"
            )
        except Exception as exc:
            db.log_event(f"Open order failed: {exc}", level="ERROR")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _daily_loss_exceeded(self, current_equity: float) -> bool:
        """Simple daily P&L check using DB trade records."""
        today = datetime.now(timezone.utc).date().isoformat()
        trades = db.get_trade_history(self.symbol, limit=50)
        daily_pnl = sum(
            t["pnl"] or 0
            for t in trades
            if t.get("closed_at", "").startswith(today)
        )
        if current_equity > 0:
            return (daily_pnl / current_equity) < -config.MAX_DAILY_LOSS_PCT
        return False


# ── Utility functions ─────────────────────────────────────────────────────────

def _candles_to_df(raw: list) -> pd.DataFrame:
    """Convert BloFin candle list to OHLCV DataFrame (newest-last)."""
    # BloFin returns newest first; reverse to chronological order
    rows = list(reversed(raw))
    df = pd.DataFrame(
        rows,
        columns=["ts", "open", "high", "low", "close", "volume", "vol_ccy"],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("datetime").sort_index()


def _extract_usdt_equity(balance_data: dict) -> float | None:
    """Extract USDT equity from BloFin balance response."""
    details = balance_data.get("details", [])
    for item in details:
        if item.get("currency") == "USDT":
            return float(item.get("equity", 0))
    # Flat structure fallback
    if "equity" in balance_data:
        return float(balance_data["equity"])
    return None


def _calc_pnl(
    direction: str,
    entry: float,
    exit_price: float,
    size: float,
) -> float:
    """
    Calculate realised P&L in USDT.

    P&L = price_change_pct × notional_value (= pct × entry × size).
    Leverage is NOT multiplied here because position sizing already
    incorporates risk: size = (equity × risk_pct) / (entry × stop_pct).
    """
    if direction == Signal.LONG:
        pct = (exit_price - entry) / entry
    else:
        pct = (entry - exit_price) / entry
    return round(pct * entry * size, 4)
