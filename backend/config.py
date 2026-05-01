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

# Portfolio-level caps (multi-symbol guard)
# These prevent all running bots from collectively over-allocating equity.
MAX_OPEN_POSITIONS: int = _env_int("MAX_OPEN_POSITIONS", 3)
MAX_MARGIN_USAGE_PCT: float = _env_float("MAX_MARGIN_USAGE_PCT", 0.40)     # 40 % of equity as margin
MAX_PORTFOLIO_RISK_PCT: float = _env_float("MAX_PORTFOLIO_RISK_PCT", 0.05)  # 5 % equity at risk across all stops
MAX_SYMBOL_EXPOSURE_PCT: float = _env_float("MAX_SYMBOL_EXPOSURE_PCT", 0.50)  # no symbol > 50 % of notional cap

if MAX_OPEN_POSITIONS < 1:
    MAX_OPEN_POSITIONS = 1
if MAX_MARGIN_USAGE_PCT <= 0 or MAX_MARGIN_USAGE_PCT > 1:
    MAX_MARGIN_USAGE_PCT = 0.40
if MAX_PORTFOLIO_RISK_PCT <= 0 or MAX_PORTFOLIO_RISK_PCT > 1:
    MAX_PORTFOLIO_RISK_PCT = 0.05
if MAX_SYMBOL_EXPOSURE_PCT <= 0 or MAX_SYMBOL_EXPOSURE_PCT > 1:
    MAX_SYMBOL_EXPOSURE_PCT = 0.50

TRADING_MARGIN_MODE: str = _env_str("TRADING_MARGIN_MODE", "isolated").lower()
TRADING_MODE: str = _env_str("TRADING_MODE", "papertrading").lower()
PAPER_START_EQUITY: float = _env_float("PAPER_START_EQUITY", 1000.0)

if TRADING_MARGIN_MODE not in {"cross", "isolated"}:
    TRADING_MARGIN_MODE = "isolated"

if TRADING_MODE not in {"papertrading", "realtrading"}:
    TRADING_MODE = "papertrading"
    LEVERAGE = 1
elif LEVERAGE > 125:
    LEVERAGE = 125

if RISK_PER_TRADE <= 0:
    RISK_PER_TRADE = 0.01
elif RISK_PER_TRADE > 1:
    RISK_PER_TRADE = 1.0

if PAPER_START_EQUITY <= 0:
    PAPER_START_EQUITY = 1000.0

# ── Copy trading defaults (overridable at runtime via /api/copytrading/config) ─
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().strip("\"'").lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return default

COPY_TRADING_ENABLED: bool = _env_bool("COPY_TRADING_ENABLED", False)
COPY_TRADING_TRADER_ID: str = _env_str("COPY_TRADING_TRADER_ID", "")

# Supported symbols (scalable)
SUPPORTED_SYMBOLS: list[str] = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "LINK-USDT",
]

# ── Per-symbol strategy overrides ─────────────────────────────────────────────
# Each entry may contain any combination of:
#   stop_loss_pct, take_profit_pct   – risk management (always honoured)
#   adx_min, rsi_pullback_max, rsi_recovery_long,
#   pullback_lookback, signal_cooldown  – signal params (override module defaults)
#   rsi_pullback_min, rsi_recovery_short – explicit SHORT RSI thresholds;
#     when absent, derived as 100 – rsi_pullback_max / 100 – rsi_recovery_long
#
# Missing keys fall back to the module-level defaults in strategy.py.
SYMBOL_PARAMS: dict[str, dict] = {
    # BTC-USDT: v7 grid-search (365-day 15m data).
    # +20.17% return, WR=48.6%, MaxDD=-7.07%
    # v8: relaxed module-level defaults; per-symbol RSI thresholds widened for
    # bull-market regimes where RSI rarely dips below 52.
    # v9: LONG thresholds raised further (60→65) because in the May-2026 bull
    # market RSI oscillated 62-80 and the v8 threshold of ≤60 was never reached
    # within the 6-bar window, producing zero LONG signals.  Explicit SHORT
    # thresholds (rsi_pullback_min=40, rsi_recovery_short=37) preserve the
    # previously-tested SHORT behaviour (mirror of the v8 LONG params: 100-60=40,
    # 100-63=37) so that raising LONG params does not accidentally loosen SHORT.
    "BTC-USDT": {
        "stop_loss_pct":       STOP_LOSS_PCT,   # 2.5%
        "take_profit_pct":     TAKE_PROFIT_PCT,  # 4.0%
        "rsi_pullback_max":    65.0,   # raised 60→65: catches RSI dips in 62-65 bull range
        "rsi_recovery_long":   68.0,   # raised 63→68: 3-pt gap above new pullback threshold
        "rsi_pullback_min":    40.0,   # explicit SHORT: preserve v8 value (100-60)
        "rsi_recovery_short":  37.0,   # explicit SHORT: preserve v8 value (100-63)
        "pullback_lookback":   6,
    },
    # ETH-USDT: grid-search (365-day 15m data).
    # ETH benefits from tighter SL and wider TP: sharp momentum bursts with
    # cleaner moves than BTC noise.
    # +25.63% return, WR=31.6%, MaxDD=-6.49%
    # v9: same LONG-threshold raise as BTC; explicit SHORT thresholds preserved.
    "ETH-USDT": {
        "stop_loss_pct":       0.015,   # 1.5% — tighter than BTC (ETH moves more cleanly)
        "take_profit_pct":     0.070,   # 7.0% — wider to capture ETH momentum bursts
        "rsi_pullback_max":    65.0,   # raised 60→65: bull-market dip detection
        "rsi_recovery_long":   68.0,   # raised 63→68: 3-pt gap above pullback threshold
        "rsi_pullback_min":    40.0,   # explicit SHORT: preserve v8 value (100-60)
        "rsi_recovery_short":  37.0,   # explicit SHORT: preserve v8 value (100-63)
        "pullback_lookback":   6,
    },
    # SOL-USDT: grid-search (365-day 15m data).
    # v8: Relaxed from v7 per-symbol params to generate more signals.
    # v9: LONG thresholds raised 50→55/53→58 for bull-market bull RSI regime.
    "SOL-USDT": {
        "stop_loss_pct":    0.015,   # 1.5%
        "take_profit_pct":  0.070,   # 7.0%
        "adx_min":          18.0,    # relaxed from 22: trade in moderately trending SOL
        "rsi_pullback_max": 55.0,    # raised 50→55: catches SOL dips in 52-55 range
        "rsi_recovery_long": 58.0,   # raised 53→58: confirm recovery (dip ≤55 then recover ≥58)
        "pullback_lookback": 8,      # widened from 5: 2-hour detection window
        "signal_cooldown":  24,      # 6-hour cooldown
    },
    # XRP-USDT: grid-search (365-day 15m data).
    # v8: Relaxed from v7 per-symbol params to generate more signals.
    # v9: LONG thresholds raised 50→55/53→58 for bull-market RSI regime.
    "XRP-USDT": {
        "stop_loss_pct":    0.015,   # 1.5%
        "take_profit_pct":  0.070,   # 7.0%
        "adx_min":          18.0,    # relaxed from 22
        "rsi_pullback_max": 55.0,    # raised 50→55: catches dips in 52-55 range
        "rsi_recovery_long": 58.0,   # raised 53→58: confirm recovery (dip ≤55 then recover ≥58)
        "pullback_lookback": 8,      # widened from 5: 2-hour detection window
        "signal_cooldown":  24,      # 6-hour cooldown
    },
    # LINK-USDT: wide grid-search (365-day 15m data).
    # v8: Relaxed from v7 per-symbol params to generate more signals.
    # v9: LONG thresholds raised 50→55/53→58 for bull-market RSI regime.
    "LINK-USDT": {
        "stop_loss_pct":    0.010,   # 1.0% — tight SL for volatile LINK
        "take_profit_pct":  0.055,   # 5.5%
        "adx_min":          14.0,    # relaxed from 18: LINK trends at lower ADX
        "rsi_pullback_max": 55.0,    # raised 50→55: catches dips in 52-55 range
        "rsi_recovery_long": 58.0,   # raised 53→58: confirm recovery (dip ≤55 then recover ≥58)
        "pullback_lookback": 8,      # widened from 5: 2-hour detection window
        "signal_cooldown":  24,      # 6-hour cooldown
    },
}


def get_symbol_params(symbol: str) -> dict:
    """Return all per-symbol strategy and risk parameters for *symbol*.

    Always contains ``stop_loss_pct`` and ``take_profit_pct``.
    May also contain strategy-level overrides: ``adx_min``,
    ``rsi_pullback_max``, ``rsi_recovery_long``, ``pullback_lookback``,
    ``signal_cooldown``.  Keys absent from the override dict fall back to
    the module-level defaults in ``strategy.py``.
    """
    return SYMBOL_PARAMS.get(symbol, {
        "stop_loss_pct":   STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
    })
