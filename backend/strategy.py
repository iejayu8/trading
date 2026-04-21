"""
strategy.py – Trend Pullback + Momentum Recovery strategy.

Timeframe  : 15 minutes
Instruments: BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, LINK-USDT
             (all symbols use the same strategy logic with per-symbol
              parameter overrides stored in config.SYMBOL_PARAMS)

Strategy: Pullback-to-EMA with RSI Recovery (v8)
─────────────────────────────────────────────────
Identifies short-term pullbacks within established trends and enters
when momentum recovers, gated by ADX trend-strength to avoid the most
extreme ranging markets.

Root-cause analysis (v5 → v6 → v7 → v8)
─────────────────────────────────────────
v5 → v6 (ADX gate + lookback tightening):
• 64 % of SL trades occurred in sub-ADX-22 environments.
• Pullback lookback of 8 bars (2 h) was too wide: stale setups.
• Raising ADX_MIN to 22 and halving lookback to 4: WR 35%→45%, return +5%→+22%.

v6 → v7 (grid-search optimization on 2025-2026 data):
• Lowering ADX_MIN from 22 → 20 captures valid trends with slightly lower ADX.
• Widening RSI_PULLBACK_MAX from 42 → 46 allows shallower pullbacks.
• Lowering RSI_RECOVERY_LONG from 52 → 49 enters near the RSI neutral zone.
• Tightening PULLBACK_LOOKBACK from 4 → 3 bars enforces freshness.

v7 → v8 (live production tuning – April 2026):
• In ranging / transitional markets the v7 conditions were almost never
  simultaneously satisfied, resulting in zero signals across 5 symbols over
  multiple days.  Root cause: the 3-bar window and deep RSI thresholds
  rarely align with the neutral-RSI pullbacks that dominate modern crypto.
• Lowering ADX_MIN from 20 → 16: trade in moderately trending environments,
  retaining the filter only against the most extreme ranging conditions.
• Widening RSI_PULLBACK_MAX from 46 → 52: accept neutral-zone dips (RSI
  briefly touches ≤ 52 in a healthy uptrend consolidation), not just deep
  oversold dips that occur infrequently.
• Raising RSI_RECOVERY_LONG from 49 → 55: the gap between pullback (≤ 52)
  and recovery (≥ 55) is maintained at 3 points, ensuring genuine momentum
  reversal is confirmed before entry.
• Widening PULLBACK_LOOKBACK from 3 → 6 bars (90 min): gives the strategy a
  wider window to catch the pullback, reducing sensitivity to exact timing.

Backtest result (365 days BTC/USDT 15m, grid search over 2 916 combinations)
──────────────────────────────────────────────────────────────────────────────
  v6 (ADX≥22, LB=4, SL=2.0%, TP=4.5%):  -13.22%, WR=28.9%, DD=-15.94%  ← 2025-26 data
  v7 (ADX≥20, LB=3, SL=2.5%, TP=4.0%):  +20.17%, WR=48.6%, DD= -7.07%  ← 2025-26 data

Entry conditions (LONG)
──────────────────────
1. ADX ≥ 16      : market is trending (not extreme chop / ranging)
2. Macro trend   : close > 200 EMA
3. Medium trend  : slow EMA (21) > trend EMA (55)
4. Fresh pullback: RSI dropped below 52 within the last 6 bars
5. Recovery      : current RSI ≥ 55 (momentum returning above neutral)
6. Price above   : close > fast EMA (9) — price reclaimed the fast EMA
7. MACD hist     : current bar ≥ −0.5 × ATR  (not in strong downtrend)
8. Volume        : current bar ≥ 0.9× 20-period SMA

Entry conditions (SHORT) are the mirror image.

Cooldown: 24-bar minimum (6 h) between entries.

Risk management
───────────────
Stop loss   : STOP_LOSS_PCT  (2.5 %) from entry price
Take profit : TAKE_PROFIT_PCT (4.0 %) from entry price  ── 1.6:1 R/R
Leverage    : 5×  (cross-margin)
Risk/trade  : 1 % of account equity
"""

from __future__ import annotations

import pandas as pd
import numpy as np

# Import policy:
# Prefer package-relative imports when running as `python -m backend.app`.
# Keep absolute fallback for direct-module contexts used by tests and some tools.
try:
    from . import config
except ImportError:
    import importlib

    config = importlib.import_module("config")

SIGNAL_COOLDOWN = 24
MIN_BARS_REQUIRED = 200
# Minimum bars between signal generation (~6 h on 15-min chart).
# Halved from 48 to 24 (v7): 6-hour cooldown doubles trade frequency without
# materially increasing correlated losses, since ADX/RSI filters still gate quality.

ADX_MIN = 16.0
# ADX must be ≥ 16 to confirm the market is in a trending state.
# Lowered from 20 (v8): the 20-threshold was too strict for post-ATH
# crypto markets that trend at ADX 15-20, producing zero signals over
# multiple days.  16 still filters extreme ranging environments (ADX < 15)
# while allowing moderately directional markets to participate.

RSI_PULLBACK_MAX = 52.0
# RSI must dip to ≤ 52 within PULLBACK_LOOKBACK bars for LONG entries.
# Raised from 46 (v8): the 46-threshold required deep oversold dips that
# rarely occur in modern trending crypto.  52 catches neutral-zone pullbacks
# (RSI briefly touches ≤ 52 during healthy uptrend consolidations), generating
# more entries without requiring extreme bearish momentum.

RSI_PULLBACK_MIN = 48.0
# Mirror threshold for SHORT entries: RSI must spike to ≥ 48 during pullback.
# Mirrors RSI_PULLBACK_MAX (100 - 52 = 48).

RSI_RECOVERY_LONG = 55.0
# RSI must recover above 55 before entering LONG, confirming momentum return.
# Raised from 49 (v8): maintains a 3-point spread above the new pullback
# threshold (dip ≤ 52, recover ≥ 55), ensuring genuine bullish momentum
# before entry rather than triggering on a flat/sideways RSI reading.

RSI_RECOVERY_SHORT = 45.0
# RSI must fall back below 45 before entering SHORT.
# Mirrors RSI_RECOVERY_LONG (100 - 55 = 45).

PULLBACK_LOOKBACK = 6
# Number of bars to look back for the RSI dip/spike (90 min on 15-min chart).
# Widened from 3 → 6 (v8): the 3-bar window was too narrow — a pullback that
# occurred 4-6 bars ago is still fresh enough to enter on recovery.  90 minutes
# doubles the detection window without introducing stale setups.

MACD_GATE_ATR_MULT = 0.5
# MACD histogram gate expressed as a multiple of ATR so the filter scales
# with the asset's price level and volatility.  A value of 0.5 means:
#   LONG  → macd_hist >= -0.5 × ATR  (reject entry when bearish momentum is extreme)
#   SHORT → macd_hist <=  0.5 × ATR  (reject entry when bullish momentum is extreme)
# Replaces the former hardcoded ±50 which was only meaningful for BTC-USDT
# ($80k price range) and a no-op for smaller-priced assets like XRP/LINK.

VOLUME_MULT = 0.9
# Volume floor: current bar must be ≥ 90 % of its SMA.
# Slightly below 1.0 to avoid missing signals on low-volume hours.


class Signal:
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to OHLCV DataFrame."""
    df = df.copy()

    df["ema_fast"]  = df["close"].ewm(span=config.FAST_EMA,  adjust=False).mean()
    df["ema_slow"]  = df["close"].ewm(span=config.SLOW_EMA,  adjust=False).mean()
    df["ema_trend"] = df["close"].ewm(span=config.TREND_EMA, adjust=False).mean()
    df["ema_200"]   = df["close"].ewm(span=200,              adjust=False).mean()

    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=config.RSI_PERIOD - 1, min_periods=config.RSI_PERIOD).mean()
    avg_loss = loss.ewm(com=config.RSI_PERIOD - 1, min_periods=config.RSI_PERIOD).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["volume_sma"] = df["volume"].rolling(config.VOLUME_SMA_PERIOD).mean()

    ema12             = df["close"].ewm(span=12, adjust=False).mean()
    ema26             = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    adx_period = 14
    up_move    = df["high"] - df["high"].shift(1)
    down_move  = df["low"].shift(1) - df["low"]
    dm_plus    = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    dm_minus   = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_w      = tr.ewm(span=adx_period, min_periods=adx_period, adjust=False).mean()
    di_plus    = (pd.Series(dm_plus,  index=df.index)
                  .ewm(span=adx_period, min_periods=adx_period, adjust=False).mean()
                  / atr_w * 100)
    di_minus   = (pd.Series(dm_minus, index=df.index)
                  .ewm(span=adx_period, min_periods=adx_period, adjust=False).mean()
                  / atr_w * 100)
    dx         = ((di_plus - di_minus).abs() / (di_plus + di_minus) * 100).fillna(0)
    df["adx"]      = dx.ewm(span=adx_period, min_periods=adx_period, adjust=False).mean()
    df["di_plus"]  = di_plus
    df["di_minus"] = di_minus

    return df


_last_signal_ts: dict[str, pd.Timestamp] = {}
# Keys are trading symbols (e.g. "BTC-USDT", "ETH-USDT").
# Values are the UTC timestamp of the last candle that generated a signal.
# A missing key means the symbol has never signalled.
#
# NOTE: Previously this stored a positional bar *index* (len(df) - 1).  That
# broke in production because the bot always fetches a fixed number of candles
# (limit=200), so len(df) - 1 was always 199.  Once a signal fired at index
# 199, every subsequent tick computed bars_since = 199 - 199 = 0, which is
# permanently less than the cooldown threshold.  Tracking timestamps instead
# of indices is robust to any fetch-window size.

_CANDLE_SECONDS = 15 * 60  # 15-minute candles = 900 s


def _get_last_signal_ts(symbol: str) -> pd.Timestamp | None:
    """Return the timestamp of the last signal candle for *symbol*, or None."""
    return _last_signal_ts.get(symbol)


def _bars_since_last_signal(symbol: str, current_ts: pd.Timestamp) -> int:
    """Return elapsed bars since the last signal for *symbol*.

    Returns a very large number when the symbol has never signalled so that the
    cooldown check always passes (i.e. the symbol is immediately eligible).
    """
    last = _get_last_signal_ts(symbol)
    if last is None:
        return SIGNAL_COOLDOWN + 1  # guaranteed to be ≥ any cooldown
    elapsed_seconds = (current_ts - last).total_seconds()
    return int(elapsed_seconds // _CANDLE_SECONDS)


def reset_signal_state(symbol: str | None = None) -> None:
    """Reset cooldown state for *symbol* (or all symbols when None)."""
    if symbol is None:
        _last_signal_ts.clear()
    else:
        _last_signal_ts.pop(symbol, None)


def generate_signal(df: pd.DataFrame, symbol: str = "BTC-USDT") -> str:
    """
    Generate a signal based on the latest completed candle.

    Returns Signal.LONG | Signal.SHORT | Signal.NONE
    """
    if len(df) < MIN_BARS_REQUIRED:
        return Signal.NONE

    sym_params = config.get_symbol_params(symbol)
    cooldown   = sym_params.get("signal_cooldown", SIGNAL_COOLDOWN)
    adx_min    = sym_params.get("adx_min", ADX_MIN)

    current_ts = df.index[-1]
    if _bars_since_last_signal(symbol, current_ts) < cooldown:
        return Signal.NONE

    last = df.iloc[-1]
    needed = ["ema_fast", "ema_slow", "ema_trend", "ema_200",
              "rsi", "volume_sma", "macd_hist", "atr", "adx"]
    if any(pd.isna(last[col]) for col in needed):
        return Signal.NONE

    # ADX gate: only trade when the market is in a trending state.
    # Entries in low-ADX choppy markets are the primary source of SL hits.
    if last["adx"] < adx_min:
        return Signal.NONE

    checks = get_signal_checks(df, sym_params)

    # ── Long: fresh pullback + recovery within uptrend ─────────────────────
    if all(checks["long_checks"].values()):
        _last_signal_ts[symbol] = current_ts
        return Signal.LONG

    # ── Short: fresh rally + rejection within downtrend ────────────────────
    if all(checks["short_checks"].values()):
        _last_signal_ts[symbol] = current_ts
        return Signal.SHORT

    return Signal.NONE


def get_signal_diagnostics(df: pd.DataFrame, symbol: str = "BTC-USDT") -> dict:
    """
    Explain current strategy state and what the bot is waiting for.

    Returns a dict with readiness flags and a human-readable waiting reason.
    """
    min_bars = MIN_BARS_REQUIRED

    sym_params = config.get_symbol_params(symbol)
    cooldown   = sym_params.get("signal_cooldown", SIGNAL_COOLDOWN)

    out = {
        "long_ready": False,
        "short_ready": False,
        "signal_hint": "WAIT",
        "waiting_for": "Collecting candles",
    }

    if len(df) < min_bars:
        out["waiting_for"] = f"Collecting candles ({len(df)}/{min_bars})"
        return out

    current_ts = df.index[-1]
    bars_since = _bars_since_last_signal(symbol, current_ts)
    cooldown_left = cooldown - bars_since
    if cooldown_left > 0:
        out["signal_hint"] = "COOLDOWN"
        out["waiting_for"] = f"Cooldown active ({cooldown_left} bars left)"
        return out

    last = df.iloc[-1]
    needed = [
        "ema_fast", "ema_slow", "ema_trend", "ema_200",
        "rsi", "volume_sma", "macd_hist", "atr", "adx",
    ]
    if any(pd.isna(last[col]) for col in needed):
        out["waiting_for"] = "Warming up indicators"
        return out

    checks = get_signal_checks(df, sym_params)
    long_checks = checks["long_checks"]
    short_checks = checks["short_checks"]

    long_ready = all(long_checks.values())
    short_ready = all(short_checks.values())
    out["long_ready"] = long_ready
    out["short_ready"] = short_ready

    if long_ready:
        out["signal_hint"] = "LONG_READY"
        out["waiting_for"] = "Long setup ready"
        return out
    if short_ready:
        out["signal_hint"] = "SHORT_READY"
        out["waiting_for"] = "Short setup ready"
        return out

    # Choose the side with fewer blockers to explain what is closest to triggering.
    long_missing = [name for name, ok in long_checks.items() if not ok]
    short_missing = [name for name, ok in short_checks.items() if not ok]
    target_side = "LONG" if len(long_missing) <= len(short_missing) else "SHORT"
    blockers = long_missing if target_side == "LONG" else short_missing

    out["signal_hint"] = f"WAIT_{target_side}"
    out["waiting_for"] = f"{target_side}: " + ", ".join(blockers[:3])
    return out


def get_signal_checks(df: pd.DataFrame, sym_params: dict | None = None) -> dict:
    """Return per-side checks and current indicator values used for entry decisions.

    *sym_params* may contain per-symbol overrides for strategy thresholds
    (adx_min, rsi_pullback_max, rsi_recovery_long, pullback_lookback).
    Falls back to module-level defaults when keys are absent.
    """
    if df.empty:
        return {
            "long_checks": {},
            "short_checks": {},
            "values": {},
        }

    if sym_params is None:
        sym_params = {}

    adx_min           = sym_params.get("adx_min",           ADX_MIN)
    rsi_pullback_max  = sym_params.get("rsi_pullback_max",  RSI_PULLBACK_MAX)
    rsi_pullback_min  = 100.0 - rsi_pullback_max
    rsi_recovery_long = sym_params.get("rsi_recovery_long", RSI_RECOVERY_LONG)
    rsi_recovery_short = 100.0 - rsi_recovery_long
    pullback_lookback  = sym_params.get("pullback_lookback", PULLBACK_LOOKBACK)

    last = df.iloc[-1]
    rsi_window = df["rsi"].iloc[-(pullback_lookback + 1):-1]
    vol_threshold = VOLUME_MULT * last["volume_sma"] if pd.notna(last["volume_sma"]) else np.nan
    vol_ok = pd.notna(last["volume"]) and pd.notna(vol_threshold) and last["volume"] >= vol_threshold

    # ATR-normalised MACD gate so the filter scales to any asset price level.
    # When ATR is unavailable (NaN/zero during warmup), use a very large gate
    # so the MACD filter is effectively disabled rather than blocking all signals.
    atr_val = float(last["atr"]) if pd.notna(last["atr"]) and float(last["atr"]) > 0 else None
    macd_gate = MACD_GATE_ATR_MULT * atr_val if atr_val is not None else float("inf")

    long_checks = {
        "ADX >= threshold": bool(last["adx"] >= adx_min),
        "Price above EMA200": bool(last["close"] > last["ema_200"]),
        "EMA21 above EMA55": bool(last["ema_slow"] > last["ema_trend"]),
        "Recent RSI pullback": bool((rsi_window <= rsi_pullback_max).any()),
        "RSI recovered": bool(last["rsi"] >= rsi_recovery_long),
        "Price above EMA9": bool(last["close"] > last["ema_fast"]),
        "MACD filter": bool(last["macd_hist"] >= -macd_gate),
        "Volume filter": bool(vol_ok),
    }

    short_checks = {
        "ADX >= threshold": bool(last["adx"] >= adx_min),
        "Price below EMA200": bool(last["close"] < last["ema_200"]),
        "EMA21 below EMA55": bool(last["ema_slow"] < last["ema_trend"]),
        "Recent RSI spike": bool((rsi_window >= rsi_pullback_min).any()),
        "RSI rejected": bool(last["rsi"] <= rsi_recovery_short),
        "Price below EMA9": bool(last["close"] < last["ema_fast"]),
        "MACD filter": bool(last["macd_hist"] <= macd_gate),
        "Volume filter": bool(vol_ok),
    }

    values = {
        "close": float(last["close"]),
        "ema_fast": float(last["ema_fast"]),
        "ema_slow": float(last["ema_slow"]),
        "ema_trend": float(last["ema_trend"]),
        "ema_200": float(last["ema_200"]),
        "rsi": float(last["rsi"]),
        "volume": float(last["volume"]),
        "volume_sma": float(last["volume_sma"]) if pd.notna(last["volume_sma"]) else None,
        "volume_threshold": float(vol_threshold) if pd.notna(vol_threshold) else None,
        "adx": float(last["adx"]),
        "macd_hist": float(last["macd_hist"]),
        "rsi_recovery_long": rsi_recovery_long,
        "rsi_recovery_short": rsi_recovery_short,
        "adx_min": adx_min,
    }

    return {
        "long_checks": long_checks,
        "short_checks": short_checks,
        "values": values,
    }


def calculate_position_size(
    equity: float,
    entry_price: float,
    leverage: int = None,
    risk_pct: float = None,
    stop_loss_pct: float = None,
) -> float:
    """
    Size the position so a SL hit costs equity × risk_pct.

    size = (equity × risk_pct) / (entry_price × stop_loss_pct)
    """
    if leverage is None:
        leverage = config.LEVERAGE
    if risk_pct is None:
        risk_pct = config.RISK_PER_TRADE
    if stop_loss_pct is None:
        stop_loss_pct = config.STOP_LOSS_PCT

    risk_amount = equity * risk_pct
    size        = risk_amount / (entry_price * stop_loss_pct)
    return max(round(size, 4), 0.001)


def calculate_sl_tp(
    entry_price: float,
    direction: str,
    stop_loss_pct: float = None,
    take_profit_pct: float = None,
) -> tuple[float, float]:
    """Return (stop_loss_price, take_profit_price)."""
    if stop_loss_pct is None:
        stop_loss_pct = config.STOP_LOSS_PCT
    if take_profit_pct is None:
        take_profit_pct = config.TAKE_PROFIT_PCT

    if direction == Signal.LONG:
        sl = entry_price * (1 - stop_loss_pct)
        tp = entry_price * (1 + take_profit_pct)
    else:
        sl = entry_price * (1 + stop_loss_pct)
        tp = entry_price * (1 - take_profit_pct)
    return round(sl, 2), round(tp, 2)
