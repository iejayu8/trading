"""
Tests for the trading strategy module.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
from strategy import (
    Signal,
    _last_signal_bar,
    SIGNAL_COOLDOWN,
    calculate_position_size,
    calculate_sl_tp,
    compute_indicators,
    generate_signal,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_ohlcv(n: int = 600, trend: str = "up") -> pd.DataFrame:
    """
    Generate synthetic OHLCV data with clear trend and deep pullbacks.

    Uses alternating trend/correction phases so RSI can dip below 42
    (needed for the pullback-to-EMA strategy to fire LONG signals).
    """
    np.random.seed(42)
    prices = [40000.0]
    drift = 200 if trend == "up" else -200 if trend == "down" else 0

    for i in range(n - 1):
        phase = i % 60
        # Deep counter-trend pullback for 15 bars every cycle so RSI can dip/spike
        if 20 <= phase < 35:
            drift_this_bar = -abs(drift) * 2
        else:
            drift_this_bar = drift
        step = np.random.normal(drift_this_bar, 150)
        prices.append(max(100, prices[-1] + step))

    df = pd.DataFrame(
        {
            "open":   [p * 0.999 for p in prices],
            "high":   [p * 1.005 for p in prices],
            "low":    [p * 0.995 for p in prices],
            "close":  prices,
            "volume": [np.random.uniform(300, 900) for _ in prices],
        }
    )
    df.index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return df


def reset_signal_state():
    """Reset the module-level cooldown tracker between tests."""
    _last_signal_bar["bar"] = -SIGNAL_COOLDOWN


# ── compute_indicators ────────────────────────────────────────────────────────

class TestComputeIndicators:
    def test_columns_added(self):
        df = make_ohlcv(300)
        result = compute_indicators(df)
        for col in ["ema_fast", "ema_slow", "ema_trend", "rsi", "volume_sma",
                    "macd", "macd_signal", "macd_hist", "atr", "adx"]:
            assert col in result.columns, f"Missing column: {col}"

    def test_ema_values_finite(self):
        df = make_ohlcv(300)
        result = compute_indicators(df)
        assert result["ema_fast"].iloc[-1] > 0
        assert result["ema_slow"].iloc[-1] > 0
        assert result["ema_trend"].iloc[-1] > 0

    def test_rsi_range(self):
        df = make_ohlcv(300)
        result = compute_indicators(df)
        rsi = result["rsi"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_adx_non_negative(self):
        df = make_ohlcv(300)
        result = compute_indicators(df)
        adx = result["adx"].dropna()
        assert (adx >= 0).all()

    def test_original_df_not_mutated(self):
        df = make_ohlcv(300)
        cols_before = set(df.columns)
        compute_indicators(df)
        assert set(df.columns) == cols_before


# ── generate_signal ───────────────────────────────────────────────────────────

class TestGenerateSignal:
    def setup_method(self):
        reset_signal_state()

    def test_none_on_short_df(self):
        df = make_ohlcv(10)
        df = compute_indicators(df)
        assert generate_signal(df) == Signal.NONE

    def test_none_below_min_bars(self):
        df = make_ohlcv(100)
        df = compute_indicators(df)
        # Not enough bars for the 220-bar minimum
        assert generate_signal(df) == Signal.NONE

    def test_long_signal_on_uptrend(self):
        """
        An uptrend with periodic pullbacks (RSI dips below 42) should
        eventually produce a LONG signal.
        """
        reset_signal_state()
        df = make_ohlcv(600, trend="up")
        df = compute_indicators(df)
        signals = [generate_signal(df.iloc[: i + 1]) for i in range(220, len(df))]
        assert Signal.LONG in signals

    def test_short_signal_on_downtrend(self):
        """
        A downtrend with periodic RSI spikes above 58 should produce SHORT signals.
        """
        reset_signal_state()
        df = make_ohlcv(600, trend="down")
        df = compute_indicators(df)
        signals = [generate_signal(df.iloc[: i + 1]) for i in range(220, len(df))]
        assert Signal.SHORT in signals

    def test_returns_valid_value(self):
        reset_signal_state()
        df = make_ohlcv(400)
        df = compute_indicators(df)
        sig = generate_signal(df)
        assert sig in (Signal.LONG, Signal.SHORT, Signal.NONE)


# ── calculate_position_size ───────────────────────────────────────────────────

class TestCalculatePositionSize:
    def test_positive_size(self):
        size = calculate_position_size(1000, 40000)
        assert size > 0

    def test_minimum_size(self):
        size = calculate_position_size(10, 40000)  # tiny equity
        assert size >= 0.001

    def test_larger_equity_larger_size(self):
        s1 = calculate_position_size(1000,  40000)
        s2 = calculate_position_size(10000, 40000)
        assert s2 > s1

    def test_custom_risk_pct(self):
        s_default = calculate_position_size(1000, 40000)
        s_double  = calculate_position_size(1000, 40000, risk_pct=0.02)
        assert abs(s_double / s_default - 2.0) < 0.01


# ── calculate_sl_tp ───────────────────────────────────────────────────────────

class TestCalculateSlTp:
    def test_long_sl_below_entry(self):
        sl, tp = calculate_sl_tp(40000, Signal.LONG)
        assert sl < 40000
        assert tp > 40000

    def test_short_sl_above_entry(self):
        sl, tp = calculate_sl_tp(40000, Signal.SHORT)
        assert sl > 40000
        assert tp < 40000

    def test_rr_ratio(self):
        """R/R should be ≥ 2:1 (current config gives ~2.25:1)."""
        entry = 40000
        sl, tp = calculate_sl_tp(entry, Signal.LONG)
        risk   = entry - sl
        reward = tp - entry
        assert reward / risk >= 2.0  # at least 2:1

    def test_stop_loss_pct_accuracy(self):
        entry = 50000
        sl, _ = calculate_sl_tp(entry, Signal.LONG)
        actual_pct = (entry - sl) / entry
        assert abs(actual_pct - config.STOP_LOSS_PCT) < 0.0001

    def test_take_profit_pct_accuracy(self):
        entry = 50000
        _, tp = calculate_sl_tp(entry, Signal.LONG)
        actual_pct = (tp - entry) / entry
        assert abs(actual_pct - config.TAKE_PROFIT_PCT) < 0.0001
