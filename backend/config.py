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

# Load credentials from credentials.env (gitignored)
_CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.env"
if _CREDENTIALS_FILE.exists():
    load_dotenv(_CREDENTIALS_FILE)

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

# Strategy parameters
FAST_EMA: int = 9
SLOW_EMA: int = 21
TREND_EMA: int = 55
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 40.0
RSI_OVERBOUGHT: float = 60.0
VOLUME_SMA_PERIOD: int = 20

# Risk management
LEVERAGE: int = int(os.getenv("TRADING_LEVERAGE", "5"))
RISK_PER_TRADE: float = float(os.getenv("RISK_PER_TRADE", "0.01"))  # 1 % of equity
STOP_LOSS_PCT: float = 0.020   # 2.0 % from entry
TAKE_PROFIT_PCT: float = 0.055  # 5.5 % from entry (~2.75:1 R/R)
MAX_DAILY_LOSS_PCT: float = 0.03  # 3 % daily drawdown guard

# Supported symbols (scalable)
SUPPORTED_SYMBOLS: list[str] = [
    "BTC-USDT",
    # "ETH-USDT",   # future
    # "SOL-USDT",   # future
]
