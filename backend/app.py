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
POST /api/logs/clear           – clear activity log
POST /api/bot/start            – start all symbol bots
POST /api/bot/start?symbol=X   – start single symbol bot
POST /api/bot/stop             – stop all symbol bots
POST /api/bot/stop?symbol=X    – stop single symbol bot
GET  /api/config               – current strategy parameters (read-only)
GET  /api/market/context       – live chart + indicators (?symbol=X)
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Import policy:
# Prefer package-relative imports when running as `python -m backend.app`.
# Keep absolute fallback for direct-module contexts used by tests and some tools.
try:
    from . import config
    from . import database as db
    from .bot import TradingBot
    from .exchange import BloFinClient
    from .strategy import compute_indicators, get_signal_checks, get_signal_diagnostics
except ImportError:
    import importlib

    config = importlib.import_module("config")
    db = importlib.import_module("database")
    TradingBot = importlib.import_module("bot").TradingBot
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


# ── Frontend ──────────────────────────────────────────────────────────────────

_STATIC_EXTENSIONS = {".css", ".js", ".html", ".ico", ".png", ".svg", ".woff", ".woff2"}


@app.get("/")
def index():
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
    limit = int(request.args.get("limit", 100))
    return jsonify(db.get_trade_history(symbol=symbol, limit=limit))


@app.get("/api/trades/open")
def api_open_trades():
    symbol = request.args.get("symbol")
    return jsonify(db.get_open_trades(symbol=symbol))


@app.get("/api/stats")
def api_stats():
    symbol = request.args.get("symbol")
    return jsonify(db.get_trade_stats(symbol=symbol))


@app.get("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 100))
    return jsonify(db.get_logs(limit=limit))


@app.post("/api/logs/clear")
def api_logs_clear():
    db.clear_logs()
    return jsonify({"ok": True, "message": "Activity log cleared"})


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

    # Stop all bots
    stopped = []
    for sym, bot in list(_bots.items()):
        if bot.is_running:
            bot.stop()
            stopped.append(sym)
    if not stopped:
        return jsonify({"ok": False, "message": "No bots are running"}), 400
    return jsonify({"ok": True, "message": f"Stopped bots: {', '.join(stopped)}"})


# ── Configuration ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    symbol = request.args.get("symbol", config.TRADING_SYMBOL)
    sym_params = config.get_symbol_params(symbol)
    # Import strategy defaults for fallback display values
    try:
        from .strategy import ADX_MIN, RSI_PULLBACK_MAX, RSI_RECOVERY_LONG, PULLBACK_LOOKBACK, SIGNAL_COOLDOWN
    except ImportError:
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

    client = BloFinClient()
    raw = client.get_candles(symbol, bar=config.TIMEFRAME, limit=limit)
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
