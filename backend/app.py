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

import threading

from flask import Flask, jsonify, request
from flask_cors import CORS

import config
import database as db
from bot import TradingBot

app = Flask(__name__)
CORS(app)

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
            "timeframe": config.TIMEFRAME,
            "leverage": config.LEVERAGE,
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
