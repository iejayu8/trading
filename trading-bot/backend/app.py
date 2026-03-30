"""
app.py – Flask REST API server.

Endpoints
──────────
GET  /api/status          – bot status, equity, last signal
GET  /api/trades          – trade history
GET  /api/trades/open     – open trades
GET  /api/stats           – performance statistics
GET  /api/logs            – bot activity log
POST /api/bot/start       – start the bot
POST /api/bot/stop        – stop the bot
GET  /api/config          – current strategy parameters (read-only)
"""

from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import config
import database as db
from bot import TradingBot
from exchange import BloFinClient
from strategy import compute_indicators, get_signal_checks, get_signal_diagnostics

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


# Single bot instance (starts stopped)
_bot: TradingBot | None = None
_bot_lock = threading.Lock()


def _get_bot() -> TradingBot:
    global _bot
    with _bot_lock:
        if _bot is None:
            _bot = TradingBot()
    return _bot


# ── Status & monitoring ───────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    status = db.get_bot_status()
    status["trading_mode"] = config.TRADING_MODE
    return jsonify(status)


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
    return jsonify(db.get_trade_stats())


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
    bot = _get_bot()
    if bot.is_running:
        return jsonify({"ok": False, "message": "Bot is already running"}), 400
    bot.start()
    return jsonify({"ok": True, "message": "Bot started"})


@app.post("/api/bot/stop")
def api_stop():
    bot = _get_bot()
    if not bot.is_running:
        return jsonify({"ok": False, "message": "Bot is not running"}), 400
    bot.stop()
    return jsonify({"ok": True, "message": "Bot stopped"})


# ── Configuration ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config():
    return jsonify(
        {
            "symbol": config.TRADING_SYMBOL,
            "trading_mode": config.TRADING_MODE,
            "timeframe": config.TIMEFRAME,
            "leverage": config.LEVERAGE,
            "paper_start_equity": config.PAPER_START_EQUITY,
            "risk_per_trade_pct": config.RISK_PER_TRADE * 100,
            "stop_loss_pct": config.STOP_LOSS_PCT * 100,
            "take_profit_pct": config.TAKE_PROFIT_PCT * 100,
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
    limit = int(request.args.get("limit", 120))
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
    diagnostics = get_signal_diagnostics(df)
    checks = get_signal_checks(df)
    values = checks.get("values", {})

    candles = []
    for ts, row in df.tail(limit).iterrows():
        candles.append(
            {
                "ts": ts.isoformat(),
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
        band = max(close * 0.002, 20.0)
        target_band = {
            "long": {"low": ema_fast - band, "high": ema_fast + band},
            "short": {"low": ema_fast - band, "high": ema_fast + band},
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
