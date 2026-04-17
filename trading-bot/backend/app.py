"""
app.py – Flask REST API server.

Endpoints
──────────
GET  /api/symbols              – list of supported trading symbols
GET  /api/status               – all symbols' bot status
GET  /api/status?symbol=X      – single symbol status
GET  /api/trades               – trade history (all or ?symbol=X)
GET  /api/trades/open          – open trades (all or ?symbol=X)
GET  /api/stats                – aggregate performance statistics
GET  /api/stats?symbol=X       – per-symbol performance statistics
GET  /api/logs                 – bot activity log
POST /api/trades/<id>/close   – manually close an open trade at current market price
POST /api/bot/start            – start all symbol bots
POST /api/bot/start?symbol=X   – start single symbol bot
POST /api/bot/stop             – stop all symbol bots
POST /api/bot/stop?symbol=X    – stop single symbol bot
GET  /api/config               – current strategy parameters (read-only)
GET  /api/market/context       – live chart + indicators (?symbol=X)
POST /api/database/reset       – wipe all trades, logs and bot status (resets statistics)
GET  /api/trading/mode         – read current trading mode (papertrading/realtrading)
POST /api/trading/mode         – switch trading mode (bots must be stopped first)
GET  /api/copytrading/config   – read copy trading settings
POST /api/copytrading/config   – update copy trading settings
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from typing import TYPE_CHECKING

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Import policy:
# Prefer package-relative imports when running as `python -m backend.app`.
# Keep absolute fallback for direct-module contexts used by tests and some tools.
try:
    from . import config  # pragma: no cover
    from . import database as db  # pragma: no cover
    from .bot import TradingBot, _extract_usdt_equity  # pragma: no cover
    from .exchange import BloFinClient  # pragma: no cover
    from .strategy import compute_indicators, get_signal_checks, get_signal_diagnostics  # pragma: no cover
except ImportError:
    import importlib

    config = importlib.import_module("config")
    db = importlib.import_module("database")
    _bot_module = importlib.import_module("bot")
    TradingBot = _bot_module.TradingBot
    _extract_usdt_equity = _bot_module._extract_usdt_equity
    BloFinClient = importlib.import_module("exchange").BloFinClient
    _strategy = importlib.import_module("strategy")
    compute_indicators = _strategy.compute_indicators
    get_signal_checks = _strategy.get_signal_checks
    get_signal_diagnostics = _strategy.get_signal_diagnostics

if TYPE_CHECKING:
    try:
        from .bot import TradingBot as TradingBotType
    except ImportError:
        from bot import TradingBot as TradingBotType

_FRONTEND_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

app = Flask(__name__)
CORS(app)

# Ensure DB tables exist regardless of how Flask is started (subprocess, embedded,
# or `flask run`).  init_db() is idempotent so calling it multiple times is safe.
# DB_PATH is already set to the correct mode-specific path by database.py's
# module-level code (derived from config.TRADING_MODE and COPY_TRADING_ENABLED).
db.init_db()
# Clear stale running=1 flags left by any previous (crashed/killed) server process.
# No bot threads survive a server restart, so the DB must reflect that.
db.reset_running_flags()


# ── Frontend ──────────────────────────────────────────────────────────────────

_STATIC_EXTENSIONS = {".css", ".js", ".html", ".ico", ".png", ".svg", ".woff", ".woff2"}


@app.get("/")
def index():
    # When running as a Home Assistant add-on, HA sets X-Ingress-Path.
    # Inject it as a <meta> tag so the frontend JS can build correct API URLs.
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    if ingress_path:
        with open(os.path.join(_FRONTEND_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        html = html.replace(
            "</head>",
            f'  <meta name="ingress-path" content="{ingress_path}">\n</head>',
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    return send_from_directory(_FRONTEND_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    _, ext = os.path.splitext(filename)
    if ext.lower() not in _STATIC_EXTENSIONS:
        from flask import abort
        abort(404)
    return send_from_directory(_FRONTEND_DIR, filename)


# ── Multi-bot registry ────────────────────────────────────────────────────────
# One TradingBot instance per supported symbol, lazily created.

_bots: dict[str, TradingBotType] = {}
_bots_lock = threading.Lock()


def _get_bot(symbol: str) -> TradingBotType:
    with _bots_lock:
        if symbol not in _bots:
            _bots[symbol] = TradingBot(symbol=symbol)
    return _bots[symbol]


def _all_bots() -> list[TradingBotType]:
    return [_get_bot(s) for s in config.SUPPORTED_SYMBOLS]


# ── Status & monitoring ───────────────────────────────────────────────────────

@app.get("/api/symbols")
def api_symbols():
    return jsonify(config.SUPPORTED_SYMBOLS)


@app.get("/api/status")
def api_status():
    symbol = request.args.get("symbol")
    if symbol:
        # Single-symbol status
        status = db.get_bot_status(symbol)
        status["symbol"] = symbol
        status["trading_mode"] = config.TRADING_MODE
        bot = _bots.get(symbol)
        status["running"] = 1 if (bot and bot.is_running) else status.get("running", 0)
        return jsonify(status)

    # All symbols – return a dict keyed by symbol
    all_status = {}
    for sym in config.SUPPORTED_SYMBOLS:
        s = db.get_bot_status(sym)
        s["symbol"] = sym
        s["trading_mode"] = config.TRADING_MODE
        bot = _bots.get(sym)
        s["running"] = 1 if (bot and bot.is_running) else s.get("running", 0)
        all_status[sym] = s
    return jsonify(all_status)


@app.get("/api/trades")
def api_trades():
    symbol = request.args.get("symbol")
    try:
        limit = int(request.args.get("limit", 100))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Invalid limit parameter"}), 400
    return jsonify(db.get_trade_history(symbol=symbol, limit=limit))


@app.get("/api/trades/open")
def api_open_trades():
    symbol = request.args.get("symbol")
    return jsonify(db.get_open_trades(symbol=symbol))


@app.post("/api/trades/<int:trade_id>/close")
def api_close_trade(trade_id: int):
    import time
    from uuid import uuid4

    trade = db.get_trade_by_id(trade_id)
    if not trade:
        return jsonify({"ok": False, "message": "Trade not found"}), 404
    if trade["status"] != "OPEN":
        return jsonify({"ok": False, "message": "Trade is not open"}), 400

    symbol = trade["symbol"]
    direction = trade["direction"]
    size = trade["size"]
    entry = float(trade["entry_price"])

    # Get current price from the last known bot status for this symbol.
    status = db.get_bot_status(symbol)
    current_price = status.get("last_price")
    if not current_price:
        return jsonify({"ok": False, "message": "Current price unavailable — start the bot first"}), 502

    current_price = float(current_price)

    if config.TRADING_MODE != "papertrading":
        try:
            client = BloFinClient()
            close_side = "sell" if direction == "LONG" else "buy"
            close_cid = f"manual-close-{symbol}-{int(time.time() * 1000)}-{uuid4().hex[:8]}"
            resp = client.place_order(symbol, close_side, "market", size, client_order_id=close_cid)
            code = str(resp.get("code", "0")) if isinstance(resp, dict) else "0"
            if code != "0":
                return jsonify({"ok": False, "message": f"Exchange rejected close (code={code}): {resp.get('msg', '')}"}), 502
        except Exception as exc:
            return jsonify({"ok": False, "message": f"Exchange error: {exc}"}), 502

    if direction == "LONG":
        pnl = round((current_price - entry) * size, 4)
    else:
        pnl = round((entry - current_price) * size, 4)

    db.close_trade(trade_id, current_price, pnl)
    db.log_event(f"Trade {trade_id} manually closed @ {current_price:.2f}  PnL={pnl:.4f} USDT")

    # Refresh equity immediately so the dashboard reflects the new balance
    # without waiting for the next background tick (up to 15 min away).
    # Mirrors what TradingBot._refresh_equity_after_close() does internally.
    try:
        if config.TRADING_MODE == "papertrading":
            stats = db.get_trade_stats(symbol)
            closed_pnl = float(stats.get("total_pnl") or 0)
            equity = config.PAPER_START_EQUITY + closed_pnl
            db.update_bot_status(symbol=symbol, equity=equity)
        else:
            # For live trading, delegate to the running bot if available so
            # it can use its authenticated client to fetch the real balance.
            bot = _bots.get(symbol)
            if bot and bot.is_running:
                bot._refresh_equity_after_close()
    except Exception:
        pass  # Non-fatal: the background tick will correct equity on its next run.

    return jsonify({"ok": True, "message": f"Trade {trade_id} closed", "pnl": pnl, "exit_price": current_price})


@app.get("/api/stats")
def api_stats():
    symbol = request.args.get("symbol")
    return jsonify(db.get_trade_stats(symbol=symbol))


@app.get("/api/logs")
def api_logs():
    try:
        limit = int(request.args.get("limit", 100))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Invalid limit parameter"}), 400
    return jsonify(db.get_logs(limit=limit))


@app.post("/api/logs/clear")
def api_logs_clear():
    db.clear_logs()
    return jsonify({"ok": True, "message": "Activity log cleared"})


@app.post("/api/database/reset")
def api_database_reset():
    """Wipe all trade history, logs and bot status so statistics start fresh.

    The bots must be stopped before calling this endpoint so in-flight bot
    threads cannot race against the DELETE statements.  This works for both
    the standalone desktop app and the Home Assistant add-on because the
    database path is resolved from ``TRADING_DB_PATH`` (or the default
    location) at import time.
    """
    # Refuse to reset while any bot is actively running.
    for sym in config.SUPPORTED_SYMBOLS:
        bot = _bots.get(sym)
        if bot and bot.is_running:
            return jsonify({"ok": False, "message": "Stop all bots before resetting the database"}), 400

    db.reset_database()
    db.log_event("Database reset – all statistics cleared")
    return jsonify({"ok": True, "message": "Database reset successfully"})


# ── Bot control ───────────────────────────────────────────────────────────────

@app.post("/api/bot/start")
def api_start():
    symbol = request.args.get("symbol")
    if symbol:
        if symbol not in config.SUPPORTED_SYMBOLS:
            return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400
        bot = _get_bot(symbol)
        if bot.is_running:
            return jsonify({"ok": False, "message": f"{symbol} bot is already running"}), 400
        bot.start()
        return jsonify({"ok": True, "message": f"{symbol} bot started"})

    # Start all bots
    started = []
    for sym in config.SUPPORTED_SYMBOLS:
        bot = _get_bot(sym)
        if not bot.is_running:
            bot.start()
            started.append(sym)
    if not started:
        return jsonify({"ok": False, "message": "All bots are already running"}), 400
    return jsonify({"ok": True, "message": f"Started bots: {', '.join(started)}"})


@app.post("/api/bot/stop")
def api_stop():
    symbol = request.args.get("symbol")
    if symbol:
        if symbol not in config.SUPPORTED_SYMBOLS:
            return jsonify({"ok": False, "message": f"Unknown symbol: {symbol}"}), 400
        bot = _bots.get(symbol)
        if not bot or not bot.is_running:
            return jsonify({"ok": False, "message": f"{symbol} bot is not running"}), 400
        bot.stop()
        return jsonify({"ok": True, "message": f"{symbol} bot stopped"})

    # Stop all bots — check both in-memory threads and any orphaned DB flags.
    stopped = []
    for sym in config.SUPPORTED_SYMBOLS:
        bot = _bots.get(sym)
        if bot and bot.is_running:
            bot.stop()
            stopped.append(sym)
        else:
            # Ensure DB is clean even if the in-memory bot is gone/stale.
            status = db.get_bot_status(sym)
            if status.get("running") == 1:
                db.update_bot_status(symbol=sym, running=0)
                stopped.append(sym)
    if not stopped:
        return jsonify({"ok": False, "message": "No bots are running"}), 400
    return jsonify({"ok": True, "message": f"Stopped bots: {', '.join(stopped)}"})


def _refresh_equity_on_mode_switch(new_mode: str) -> None:
    """Refresh equity in bot_status for every symbol right after a mode switch.

    For real trading: fetches the live USDT balance from the exchange once and
    applies it to all symbols so the dashboard is never stuck on a stale value.
    For paper trading: recalculates from closed-trade PnL stored in the DB.
    Failures are logged and silently swallowed – the next bot tick will correct
    the value anyway.
    """
    real_equity = None
    if new_mode == "realtrading":
        try:
            client = BloFinClient()
            balance_data = client.get_balance()
            real_equity = _extract_usdt_equity(balance_data)
        except Exception as exc:  # noqa: BLE001
            db.log_event(
                f"Could not fetch real balance on mode switch: {exc}",
                level="WARNING",
            )

    for sym in config.SUPPORTED_SYMBOLS:
        try:
            if new_mode == "papertrading":
                stats = db.get_trade_stats(sym)
                closed_pnl = float(stats.get("total_pnl") or 0)
                equity = config.PAPER_START_EQUITY + closed_pnl
                db.update_bot_status(symbol=sym, equity=equity)
            elif real_equity is not None:
                db.update_bot_status(symbol=sym, equity=real_equity)
            else:
                # Exchange call failed – clear stale paper equity so the
                # dashboard shows '–' instead of a misleading value.
                db.update_bot_status(symbol=sym, equity=None)
        except Exception as exc:  # noqa: BLE001
            db.log_event(
                f"Could not update equity for {sym} on mode switch: {exc}",
                level="WARNING",
            )


# ── Trading mode ──────────────────────────────────────────────────────────────

@app.get("/api/trading/mode")
def api_trading_mode_get():
    """Return the current trading mode (papertrading or realtrading)."""
    return jsonify({"ok": True, "mode": config.TRADING_MODE})


@app.post("/api/trading/mode")
def api_trading_mode_set():
    """Switch between papertrading and realtrading at runtime.

    Expects JSON body: ``{"mode": "papertrading"}`` or ``{"mode": "realtrading"}``.
    All bots must be stopped before switching modes.
    """
    body = request.get_json(silent=True) or {}
    new_mode = str(body.get("mode", "")).strip().lower()
    if new_mode not in {"papertrading", "realtrading"}:
        return jsonify({"ok": False, "message": "mode must be 'papertrading' or 'realtrading'"}), 400

    if new_mode == config.TRADING_MODE:
        return jsonify({"ok": True, "mode": new_mode, "message": "Already in this mode"})

    # Require all bots to be stopped before switching trading mode.
    any_running = any(b.is_running for b in _bots.values())
    if any_running:
        return jsonify({"ok": False, "message": "Stop all bots before switching trading mode"}), 409

    old_mode = config.TRADING_MODE
    config.TRADING_MODE = new_mode
    # Recreate bot instances so they pick up the new mode on next start.
    _bots.clear()
    # Switch to the mode-specific database so all subsequent reads/writes
    # target the correct data store.
    db.switch_db(new_mode, config.COPY_TRADING_ENABLED)
    db.log_event(f"Trading mode changed from {old_mode} to {new_mode}")
    # Immediately refresh equity for all symbols so the dashboard shows the
    # correct balance without waiting for the next bot tick.
    _refresh_equity_on_mode_switch(new_mode)
    return jsonify({"ok": True, "mode": new_mode})


# ── Configuration ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    symbol = request.args.get("symbol", config.TRADING_SYMBOL)
    sym_params = config.get_symbol_params(symbol)
    # Import strategy defaults for fallback display values
    try:
        from .strategy import ADX_MIN, RSI_PULLBACK_MAX, RSI_RECOVERY_LONG, PULLBACK_LOOKBACK, SIGNAL_COOLDOWN
    except ImportError:
        import importlib
        _s = importlib.import_module("strategy")
        ADX_MIN = _s.ADX_MIN
        RSI_PULLBACK_MAX = _s.RSI_PULLBACK_MAX
        RSI_RECOVERY_LONG = _s.RSI_RECOVERY_LONG
        PULLBACK_LOOKBACK = _s.PULLBACK_LOOKBACK
        SIGNAL_COOLDOWN = _s.SIGNAL_COOLDOWN
    return jsonify(
        {
            "symbol": symbol,
            "trading_mode": config.TRADING_MODE,
            "timeframe": config.TIMEFRAME,
            "leverage": config.LEVERAGE,
            "paper_start_equity": config.PAPER_START_EQUITY,
            "risk_per_trade_pct": config.RISK_PER_TRADE * 100,
            "stop_loss_pct": sym_params["stop_loss_pct"] * 100,
            "take_profit_pct": sym_params["take_profit_pct"] * 100,
            "adx_min": sym_params.get("adx_min", ADX_MIN),
            "rsi_pullback_max": sym_params.get("rsi_pullback_max", RSI_PULLBACK_MAX),
            "rsi_recovery_long": sym_params.get("rsi_recovery_long", RSI_RECOVERY_LONG),
            "pullback_lookback": sym_params.get("pullback_lookback", PULLBACK_LOOKBACK),
            "signal_cooldown": sym_params.get("signal_cooldown", SIGNAL_COOLDOWN),
            "fast_ema": config.FAST_EMA,
            "slow_ema": config.SLOW_EMA,
            "trend_ema": config.TREND_EMA,
            "rsi_period": config.RSI_PERIOD,
            "rsi_oversold": config.RSI_OVERSOLD,
            "rsi_overbought": config.RSI_OVERBOUGHT,
            "volume_sma_period": config.VOLUME_SMA_PERIOD,
            "supported_symbols": config.SUPPORTED_SYMBOLS,
        }
    )


@app.get("/api/market/context")
def api_market_context():
    symbol = request.args.get("symbol", config.TRADING_SYMBOL)
    try:
        limit = int(request.args.get("limit", 120))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Invalid limit parameter"}), 400
    limit = max(60, min(limit, 200))

    # Always fetch at least MIN_BARS_REQUIRED candles so get_signal_diagnostics()
    # can compute accurate status. The display-level limit only controls how many
    # candles are included in the chart response, not how many are analysed.
    _MIN_BARS = 200  # mirrors strategy.MIN_BARS_REQUIRED
    fetch_limit = max(limit, _MIN_BARS)

    client = BloFinClient()
    raw = client.get_candles(symbol, bar=config.TIMEFRAME, limit=fetch_limit)
    if not raw:
        return jsonify({"ok": False, "message": "No market candles available"}), 502

    rows = list(reversed(raw))
    if len(rows[0]) < 7:
        return jsonify({"ok": False, "message": "Unexpected candle payload"}), 502

    import pandas as pd
    df_raw = pd.DataFrame(rows)
    df = df_raw.iloc[:, :7].copy()
    df.columns = ["ts", "open", "high", "low", "close", "volume", "vol_ccy"]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()

    df = compute_indicators(df)
    sym_params   = config.get_symbol_params(symbol)
    diagnostics  = get_signal_diagnostics(df, symbol=symbol)
    checks       = get_signal_checks(df, sym_params)
    values       = checks.get("values", {})

    candles = []
    for ts, row in df.tail(limit).iterrows():
        ts_iso = ts.isoformat() if isinstance(ts, pd.Timestamp) else str(ts)
        candles.append(
            {
                "ts": ts_iso,
                "close": float(row["close"]),
                "ema_fast": float(row["ema_fast"]) if pd.notna(row["ema_fast"]) else None,
                "ema_slow": float(row["ema_slow"]) if pd.notna(row["ema_slow"]) else None,
                "ema_trend": float(row["ema_trend"]) if pd.notna(row["ema_trend"]) else None,
                "ema_200": float(row["ema_200"]) if pd.notna(row["ema_200"]) else None,
                "rsi": float(row["rsi"]) if pd.notna(row["rsi"]) else None,
                "volume": float(row["volume"]) if pd.notna(row["volume"]) else None,
                "volume_sma": float(row["volume_sma"]) if pd.notna(row["volume_sma"]) else None,
            }
        )

    close = values.get("close")
    ema_fast = values.get("ema_fast")
    target_band = None
    if close is not None and ema_fast is not None:
        # Use a pure percentage band (0.2 % of price) so the zone scales
        # correctly for every symbol — avoids the old $20 floor that was
        # disproportionately wide relative to ETH's price.
        band = close * 0.002
        target_band = {
            # Long zone: reclaim area above EMA9.
            "long": {"low": ema_fast, "high": ema_fast + band},
            # Short zone: rejection area below EMA9.
            "short": {"low": ema_fast - band, "high": ema_fast},
        }

    return jsonify(
        {
            "ok": True,
            "symbol": symbol,
            "timeframe": config.TIMEFRAME,
            "candles": candles,
            "diagnostics": diagnostics,
            "long_checks": checks.get("long_checks", {}),
            "short_checks": checks.get("short_checks", {}),
            "values": values,
            "target_band": target_band,
        }
    )


# ── Copy trading configuration ────────────────────────────────────────────────

@app.get("/api/copytrading/config")
def api_copytrading_get():
    """Return the current copy trading configuration."""
    cfg = db.get_copy_trading_config()
    return jsonify({"ok": True, "enabled": cfg["enabled"], "trader_id": cfg["trader_id"]})


@app.post("/api/copytrading/config")
def api_copytrading_set():
    """Update copy trading settings.

    Expects JSON body: ``{"enabled": true/false, "trader_id": "..."}``
    When *enabled* is ``true``, *trader_id* must be a non-empty string.

    If the copy-trading toggle changes, the active database is switched
    so each mode combination keeps its own isolated history.  All bots
    must be stopped before toggling.
    """
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled", False))
    trader_id = str(body.get("trader_id", "") or "").strip()

    if enabled and not trader_id:
        return jsonify({"ok": False, "message": "trader_id is required when copy trading is enabled"}), 400

    # When the copy-trading state changes, switch to the corresponding
    # database so data stays isolated per mode.
    toggling = enabled != config.COPY_TRADING_ENABLED
    if toggling:
        any_running = any(b.is_running for b in _bots.values())
        if any_running:
            return jsonify({"ok": False, "message": "Stop all bots before switching strategy mode"}), 409
        config.COPY_TRADING_ENABLED = enabled
        _bots.clear()
        db.switch_db(config.TRADING_MODE, enabled)

    db.set_copy_trading_config(enabled=enabled, trader_id=trader_id)
    db.log_event(
        f"Copy trading {'ENABLED (trader: ' + trader_id + ')' if enabled else 'DISABLED'}"
    )
    return jsonify({"ok": True, "enabled": enabled, "trader_id": trader_id})


# ── Entry point ───────────────────────────────────────────────────────────────

def _port_is_free(port: int) -> bool:
    """Return True if nothing is listening on *port* on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":  # pragma: no cover
    if not _port_is_free(5000):
        print(
            "ERROR: Another instance of the trading bot is already running on port 5000.\n"
            "Close it before starting a new one.",
            file=sys.stderr,
        )
        sys.exit(1)
    db.switch_db(config.TRADING_MODE, config.COPY_TRADING_ENABLED)
    db.log_event("Server started")
    # Auto-start all symbol bots so the bot runs immediately on addon init.
    for _sym in config.SUPPORTED_SYMBOLS:
        _bot = _get_bot(_sym)
        _bot.start()
        db.log_event(f"Auto-started bot for {_sym}")
    app.run(host="0.0.0.0", port=5000, debug=False)
