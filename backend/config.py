"""
config.py – Credentials and configuration loader.

The BloFin secret key is stored base64-encoded in credentials.env
to avoid keeping it in plain text. This module decodes it at runtime
so it is never written to disk in its raw form.
"""

import base64
import os
from pathlib import Path

from dotenv import load_dotenv

# Load credentials from credentials.env (gitignored), with env override for containers.
_DEFAULT_CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.env"
_CREDENTIALS_FILE = Path(os.getenv("TRADING_CREDENTIALS_FILE", str(_DEFAULT_CREDENTIALS_FILE)))
if _CREDENTIALS_FILE.exists():
    # credentials.env is the source of truth for runtime bot settings.
    load_dotenv(_CREDENTIALS_FILE, override=True)

# ── BloFin API credentials ────────────────────────────────────────────────────
BLOFIN_API_KEY: str = os.getenv("BLOFIN_API_KEY", "")
_SECRET_B64: str = os.getenv("BLOFIN_API_SECRET_B64", "")
BLOFIN_API_PASSPHRASE: str = os.getenv("BLOFIN_API_PASSPHRASE", "")

# Decode the base64-encoded secret at runtime only
def get_api_secret() -> str:
    """Return the decoded BloFin API secret. Never stored in plain text."""
    if not _SECRET_B64:
        return ""
    try:
        return base64.b64decode(_SECRET_B64).decode()
    except Exception:
        return _SECRET_B64  # fall back if already plain-text (dev only)


# ── Trading parameters ────────────────────────────────────────────────────────
TRADING_SYMBOL: str = os.getenv("TRADING_SYMBOL", "BTC-USDT")
TIMEFRAME: str = "15m"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name, default)
    if raw is None:
        return default
    # Normalize common .env styles like quoted values and trailing spaces.
    return str(raw).strip().strip("\"'").strip()

# Strategy parameters
FAST_EMA: int = 9
SLOW_EMA: int = 21
TREND_EMA: int = 55
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 40.0
RSI_OVERBOUGHT: float = 60.0
VOLUME_SMA_PERIOD: int = 20

# Risk management
LEVERAGE: int = _env_int("TRADING_LEVERAGE", 5)
RISK_PER_TRADE: float = _env_float("RISK_PER_TRADE", 0.01)  # 1 % of equity
STOP_LOSS_PCT: float = 0.025   # 2.5 % from entry  (optimized v7: wider SL reduces premature stops)
TAKE_PROFIT_PCT: float = 0.040  # 4.0 % from entry (~1.6:1 R/R, tighter TP raises win rate)
MAX_DAILY_LOSS_PCT: float = 0.03  # 3 % daily drawdown guard
TRADING_MARGIN_MODE: str = _env_str("TRADING_MARGIN_MODE", "isolated").lower()
TRADING_MODE: str = _env_str("TRADING_MODE", "realtrading").lower()
PAPER_START_EQUITY: float = _env_float("PAPER_START_EQUITY", 1000.0)

if TRADING_MARGIN_MODE not in {"cross", "isolated"}:
    TRADING_MARGIN_MODE = "isolated"

if TRADING_MODE not in {"papertrading", "realtrading"}:
    TRADING_MODE = "realtrading"

if LEVERAGE < 1:
    LEVERAGE = 1
elif LEVERAGE > 125:
    LEVERAGE = 125

if RISK_PER_TRADE <= 0:
    RISK_PER_TRADE = 0.01
elif RISK_PER_TRADE > 1:
    RISK_PER_TRADE = 1.0

if PAPER_START_EQUITY <= 0:
    PAPER_START_EQUITY = 1000.0

# Supported symbols (scalable)
SUPPORTED_SYMBOLS: list[str] = [
    "BTC-USDT",
    "ETH-USDT",
]

# ── Per-symbol strategy overrides ─────────────────────────────────────────────
# ETH-USDT optimized parameters (grid-search on 365-day 15m data).
# Same strategy logic as BTC-USDT; ETH benefits from tighter SL and higher TP
# due to its larger intra-day volatility relative to price moves.
SYMBOL_PARAMS: dict[str, dict] = {
    "BTC-USDT": {
        "stop_loss_pct":   STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
    },
    # ETH-USDT optimized parameters (grid-search on 365-day 15m data).
    # ETH benefits from tighter SL (1.5%) and much wider TP (7.0%) because
    # ETH has sharper momentum bursts relative to noise — the 4.67:1 R/R
    # compensates for the lower 31.6% win rate.
    # Backtest result: +25.63% return, WR=31.6%, MaxDD=-6.49%
    "ETH-USDT": {
        "stop_loss_pct":   0.015,   # 1.5% — tighter than BTC (ETH moves more cleanly)
        "take_profit_pct": 0.070,   # 7.0% — wider to capture ETH momentum bursts
    },
}


def get_symbol_params(symbol: str) -> dict:
    """Return stop_loss_pct and take_profit_pct for *symbol*."""
    return SYMBOL_PARAMS.get(symbol, {
        "stop_loss_pct":   STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
    })
