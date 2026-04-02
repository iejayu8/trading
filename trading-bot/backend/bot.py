"""
bot.py – Core trading bot engine.

The bot runs as a background thread, waking every 15 minutes
(aligned to the candle close) to evaluate signals and manage positions.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd

# Import policy:
# Prefer package-relative imports when running as `python -m backend.app`.
# Keep absolute fallback for direct-module contexts used by tests and some tools.
try:
    from . import config
    from . import database as db
    from .exchange import BloFinClient
    from .strategy import (
        Signal,
        calculate_position_size,
        calculate_sl_tp,
        compute_indicators,
        get_signal_diagnostics,
        generate_signal,
        reset_signal_state,
    )
except ImportError:
    import importlib

    config = importlib.import_module("config")
    db = importlib.import_module("database")
    BloFinClient = importlib.import_module("exchange").BloFinClient
    _strategy = importlib.import_module("strategy")
    Signal = _strategy.Signal
    calculate_position_size = _strategy.calculate_position_size
    calculate_sl_tp = _strategy.calculate_sl_tp
    compute_indicators = _strategy.compute_indicators
    get_signal_diagnostics = _strategy.get_signal_diagnostics
    generate_signal = _strategy.generate_signal
    reset_signal_state = _strategy.reset_signal_state


class TradingBot:
    """15-minute candle trading bot for BloFin futures."""

    CANDLE_SECONDS = 15 * 60  # 900 s
    MAX_API_RETRIES = 3
    RETRY_BASE_SECONDS = 0.7

    def __init__(self, symbol: str = None) -> None:
        self.symbol = symbol or config.TRADING_SYMBOL
        self.leverage = config.LEVERAGE
        self.trading_mode = config.TRADING_MODE
        self.paper_trading = self.trading_mode == "papertrading"
        self._client = BloFinClient()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Per-symbol risk parameters (allows different SL/TP per asset)
        sym_params = config.get_symbol_params(self.symbol)
        self._stop_loss_pct = sym_params["stop_loss_pct"]
        self._take_profit_pct = sym_params["take_profit_pct"]

        db.init_db()
        db.update_bot_status(symbol=self.symbol, running=0)
        db.log_event(
            f"Bot initialised for {self.symbol} (leverage {self.leverage}x, mode {self.trading_mode}, "
            f"SL {self._stop_loss_pct*100:.1f}%, TP {self._take_profit_pct*100:.1f}%)"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
        db.update_bot_status(symbol=self.symbol, running=1)
        db.log_event(f"Bot started ({self.symbol})")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop_event.set()

        # Join briefly for a cleaner shutdown without blocking too long.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

        db.update_bot_status(symbol=self.symbol, running=0)
        db.log_event(f"Bot stopped ({self.symbol})")

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
            if self._stop_event.wait(timeout=sleep_secs):
                break

        db.log_event("Trading loop exited")

    def _tick(self) -> None:
        """Evaluate market and act on one candle close."""
        db.log_event(f"Tick – fetching candles for {self.symbol}")

        # ── Fetch OHLCV data ─────────────────────────────────────────────────
        raw = self._call_with_retries(
            self._client.get_candles,
            self.symbol,
            bar=config.TIMEFRAME,
            limit=200,
            label="get_candles",
        )
        if not raw:
            db.log_event("No candle data received", level="WARNING")
            return

        df = _candles_to_df(raw)
        df = compute_indicators(df)

        # Persist strategy diagnostics so UI can show what entry conditions are pending.
        diag = get_signal_diagnostics(df, symbol=self.symbol)
        db.update_bot_status(
            symbol=self.symbol,
            signal_hint=diag.get("signal_hint", "WAIT"),
            waiting_for=diag.get("waiting_for", "Collecting candles"),
            long_ready=1 if diag.get("long_ready") else 0,
            short_ready=1 if diag.get("short_ready") else 0,
        )

        last_price = float(df["close"].iloc[-1])
        db.update_bot_status(symbol=self.symbol, last_price=last_price)

        # ── Account equity ───────────────────────────────────────────────────
        if self.paper_trading:
            equity = self._paper_equity()
        else:
            try:
                balance_data = self._call_with_retries(
                    self._client.get_balance,
                    label="get_balance",
                )
                equity = _extract_usdt_equity(balance_data)
            except Exception:
                equity = None

        if equity is not None:
            db.update_bot_status(symbol=self.symbol, equity=equity)

        # ── Daily loss guard ─────────────────────────────────────────────────
        if equity is not None and self._daily_loss_exceeded(equity):
            db.log_event("Daily loss limit reached – skipping signal", level="WARNING")
            return

        # ── Manage existing position ──────────────────────────────────────────
        open_trades = db.get_open_trades(symbol=self.symbol)
        exchange_has_pos = False if self.paper_trading else self._has_exchange_open_position()
        if not self.paper_trading:
            open_trades = self._reconcile_local_open_trades(open_trades, exchange_has_pos, last_price)
        self._manage_open_trades(open_trades, last_price)

        # ── Generate signal ───────────────────────────────────────────────────
        signal = generate_signal(df, symbol=self.symbol)
        db.update_bot_status(symbol=self.symbol, last_signal=signal)
        db.log_event(f"Signal: {signal} @ {last_price:.2f}")

        # ── Enter new position ────────────────────────────────────────────────
        if signal != Signal.NONE and not open_trades and not exchange_has_pos and equity:
            if self._portfolio_allows_entry(equity, last_price):
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
                reason = "TP" if hit_tp else "SL"
                if not self.paper_trading:
                    # Keep local state OPEN unless exchange confirms the close order.
                    try:
                        close_side = "sell" if direction == Signal.LONG else "buy"
                        close_cid = f"close-{self.symbol}-{int(time.time() * 1000)}-{uuid4().hex[:8]}"
                        resp = self._call_with_retries(
                            self._client.place_order,
                            self.symbol,
                            close_side,
                            "market",
                            size,
                            client_order_id=close_cid,
                            label="close_order",
                        )
                        code = str(resp.get("code", "0")) if isinstance(resp, dict) else "0"
                        if code != "0":
                            db.log_event(
                                f"Close order rejected for trade {trade['id']} (code={code}, msg={resp.get('msg')})",
                                level="ERROR",
                            )
                            continue
                    except Exception as exc:
                        db.log_event(f"Close order failed for trade {trade['id']}: {exc}", level="ERROR")
                        continue

                pnl = _calc_pnl(direction, entry, current_price, size)
                db.close_trade(trade["id"], current_price, pnl)
                db.log_event(
                    f"Trade {trade['id']} closed ({reason}) "
                    f"@ {current_price:.2f}  PnL={pnl:.4f} USDT"
                )

                if self.paper_trading:
                    db.log_event(f"[PAPER] Simulated close for trade {trade['id']}")

    def _portfolio_allows_entry(self, equity: float, new_price: float) -> bool:
        """Return True if opening a new position passes all portfolio-level caps.

        Checks four limits (all configurable via env vars):
          1. MAX_OPEN_POSITIONS  – hard cap on simultaneous open trades.
          2. MAX_MARGIN_USAGE_PCT – total margin in use must not exceed x% of equity.
          3. MAX_PORTFOLIO_RISK_PCT – total worst-case SL loss across all open trades
                                      must not exceed x% of equity.
          4. MAX_SYMBOL_EXPOSURE_PCT – no single symbol's notional may exceed x% of
                                        the allowed notional cap.
        """
        all_open = db.get_open_trades()  # across ALL symbols

        # 1. Max simultaneous positions
        if len(all_open) >= config.MAX_OPEN_POSITIONS:
            db.log_event(
                f"Portfolio cap: MAX_OPEN_POSITIONS ({config.MAX_OPEN_POSITIONS}) reached "
                f"– skipping entry for {self.symbol}",
                level="WARNING",
            )
            return False

        # Aggregate existing exposure
        total_notional = 0.0
        total_margin = 0.0
        total_risk = 0.0
        symbol_notional: dict[str, float] = {}

        for t in all_open:
            notional = t["size"] * t["entry_price"]
            margin = notional / self.leverage
            # Risk = distance from entry to SL × size
            risk = t["size"] * abs(t["entry_price"] - t["sl_price"])
            total_notional += notional
            total_margin += margin
            total_risk += risk
            sym = t.get("symbol", "")
            symbol_notional[sym] = symbol_notional.get(sym, 0.0) + notional

        # Values for the prospective new trade
        new_risk = equity * config.RISK_PER_TRADE            # by construction of calculate_position_size
        new_notional = new_risk / self._stop_loss_pct        # size * price = risk / sl_pct
        new_margin = new_notional / self.leverage

        # 2. Margin cap
        margin_cap = equity * config.MAX_MARGIN_USAGE_PCT
        if total_margin + new_margin > margin_cap:
            db.log_event(
                f"Portfolio cap: margin {total_margin + new_margin:.2f} would exceed "
                f"{margin_cap:.2f} ({config.MAX_MARGIN_USAGE_PCT*100:.0f}% of equity) "
                f"– skipping entry for {self.symbol}",
                level="WARNING",
            )
            return False

        # 3. Portfolio risk cap
        risk_cap = equity * config.MAX_PORTFOLIO_RISK_PCT
        if total_risk + new_risk > risk_cap:
            db.log_event(
                f"Portfolio cap: risk {total_risk + new_risk:.2f} would exceed "
                f"{risk_cap:.2f} ({config.MAX_PORTFOLIO_RISK_PCT*100:.0f}% of equity) "
                f"– skipping entry for {self.symbol}",
                level="WARNING",
            )
            return False

        # 4. Per-symbol notional concentration
        notional_cap = equity * config.MAX_MARGIN_USAGE_PCT * self.leverage  # total allowed notional
        sym_notional_after = symbol_notional.get(self.symbol, 0.0) + new_notional
        sym_limit = notional_cap * config.MAX_SYMBOL_EXPOSURE_PCT
        if sym_notional_after > sym_limit:
            db.log_event(
                f"Portfolio cap: {self.symbol} notional {sym_notional_after:.2f} would exceed "
                f"per-symbol limit {sym_limit:.2f} ({config.MAX_SYMBOL_EXPOSURE_PCT*100:.0f}% of cap) "
                f"– skipping entry for {self.symbol}",
                level="WARNING",
            )
            return False

        return True

    def _enter_trade(
        self, signal: str, price: float, equity: float
    ) -> None:
        size = calculate_position_size(
            equity, price,
            stop_loss_pct=self._stop_loss_pct,
        )
        sl, tp = calculate_sl_tp(price, signal,
                                  stop_loss_pct=self._stop_loss_pct,
                                  take_profit_pct=self._take_profit_pct)
        side = "buy" if signal == Signal.LONG else "sell"
        client_order_id = f"bot-{self.symbol}-{int(time.time() * 1000)}-{uuid4().hex[:8]}"

        if self.paper_trading:
            trade_id = db.open_trade(
                self.symbol, signal, price, size, sl, tp, self.leverage
            )
            db.log_event(
                f"[PAPER] Trade {trade_id} opened: {signal} {size} {self.symbol} "
                f"@ {price:.2f}  SL={sl}  TP={tp}"
            )
            return

        # Set leverage first
        try:
            self._call_with_retries(
                self._client.set_leverage,
                self.symbol,
                self.leverage,
                label="set_leverage",
            )
        except Exception as exc:
            db.log_event(f"Set leverage failed: {exc}", level="WARNING")

        # Place order on exchange first; only persist local OPEN trade after confirmation.
        try:
            resp = self._call_with_retries(
                self._client.place_order,
                self.symbol,
                side,
                "market",
                size,
                sl_price=sl,
                tp_price=tp,
                client_order_id=client_order_id,
                label="place_order",
            )

            code = str(resp.get("code", "0")) if isinstance(resp, dict) else "0"
            if code != "0":
                raise RuntimeError(f"Exchange order rejected (code={code}, msg={resp.get('msg')})")

            trade_id = db.open_trade(
                self.symbol, signal, price, size, sl, tp, self.leverage
            )
            db.log_event(
                f"Trade {trade_id} opened: {signal} {size} {self.symbol} "
                f"@ {price:.2f}  SL={sl}  TP={tp}  CID={client_order_id}"
            )
        except Exception as exc:
            db.log_event(f"Open order failed: {exc}", level="ERROR")

    def _call_with_retries(self, fn, *args, label: str = "api_call", **kwargs):
        """Retry transient API calls with linear backoff."""
        last_exc = None
        for attempt in range(1, self.MAX_API_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self.MAX_API_RETRIES:
                    break
                wait_s = self.RETRY_BASE_SECONDS * attempt
                db.log_event(
                    f"{label} failed (attempt {attempt}/{self.MAX_API_RETRIES}): {exc}. Retrying in {wait_s:.1f}s",
                    level="WARNING",
                )
                # Respect stop requests during retry sleeps.
                if self._stop_event.wait(timeout=wait_s):
                    raise RuntimeError("Bot stopping") from exc

        raise RuntimeError(f"{label} failed after {self.MAX_API_RETRIES} attempts") from last_exc

    def _has_exchange_open_position(self) -> bool:
        """Return True if exchange reports any non-zero position for symbol."""
        try:
            positions = self._call_with_retries(
                self._client.get_positions,
                self.symbol,
                label="get_positions",
            )
        except Exception as exc:
            db.log_event(f"Position sync failed: {exc}", level="WARNING")
            return False

        for p in positions or []:
            raw_pos = p.get("positions", p.get("size", 0))
            try:
                if abs(float(raw_pos)) > 0:
                    return True
            except Exception:
                continue
        return False

    def _reconcile_local_open_trades(
        self,
        local_open_trades: list[dict],
        exchange_has_position: bool,
        mark_price: float,
    ) -> list[dict]:
        """Keep local OPEN trades aligned with exchange reality."""
        if local_open_trades and not exchange_has_position:
            db.log_event(
                "Reconciliation: local OPEN trades found but exchange has no open position. Closing stale local trades.",
                level="WARNING",
            )
            for trade in local_open_trades:
                pnl = _calc_pnl(trade["direction"], trade["entry_price"], mark_price, trade["size"])
                db.close_trade(trade["id"], mark_price, pnl)
            return []

        if exchange_has_position and not local_open_trades:
            db.log_event(
                "Reconciliation: exchange position exists but no local OPEN trade. New entries will be blocked.",
                level="WARNING",
            )

        return local_open_trades

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _daily_loss_exceeded(self, current_equity: float) -> bool:
        """Simple daily P&L check using DB trade records for this symbol."""
        today = datetime.now(timezone.utc).date().isoformat()
        trades = db.get_trade_history(self.symbol, limit=50)
        daily_pnl = sum(
            t["pnl"] or 0
            for t in trades
            if (t.get("closed_at") or "").startswith(today)
        )
        if current_equity > 0:
            return (daily_pnl / current_equity) < -config.MAX_DAILY_LOSS_PCT
        return False

    def _paper_equity(self) -> float:
        """Simulated equity for paper mode: start_equity + closed PnL for this symbol."""
        stats = db.get_trade_stats(symbol=self.symbol)
        total_pnl = float(stats.get("total_pnl") or 0)
        return float(config.PAPER_START_EQUITY) + total_pnl


# ── Utility functions ─────────────────────────────────────────────────────────

def _candles_to_df(raw: list) -> pd.DataFrame:
    """Convert BloFin candle list to OHLCV DataFrame (newest-last)."""
    # BloFin returns newest first; reverse to chronological order
    rows = list(reversed(raw))

    # BloFin candle payload width can vary across API versions (e.g. 7 or 9 fields).
    # Keep only the first 7 columns we use: ts, open, high, low, close, volume, vol_ccy.
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "vol_ccy"])

    if len(rows[0]) < 7:
        raise ValueError(f"Unexpected candle row width: {len(rows[0])}")

    df_raw = pd.DataFrame(rows)
    df = df_raw.iloc[:, :7].copy()
    df.columns = ["ts", "open", "high", "low", "close", "volume", "vol_ccy"]
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
