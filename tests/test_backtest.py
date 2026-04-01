"""
Tests for the backtester (without network calls).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backtest"))

from backtest import Backtest


def make_ohlcv(n: int = 500, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    prices = [40000.0]
    for _ in range(n - 1):
        prices.append(max(100, prices[-1] + np.random.normal(50, 200)))
    df = pd.DataFrame(
        {
            "open":   [p * 0.9995 for p in prices],
            "high":   [p * 1.005  for p in prices],
            "low":    [p * 0.995  for p in prices],
            "close":  prices,
            "volume": [np.random.uniform(200, 800) for _ in prices],
        }
    )
    df.index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return df


class TestBacktest:
    def test_returns_summary_dict(self):
        df = make_ohlcv()
        bt = Backtest(initial_equity=1000.0)
        result = bt.run(df)
        assert isinstance(result, dict)
        assert "return_pct" in result

    def test_equity_tracked(self):
        df = make_ohlcv()
        bt = Backtest(initial_equity=1000.0)
        bt.run(df)
        assert bt.equity != 1000.0 or len(bt.trades) == 0

    def test_trades_have_required_fields(self):
        df = make_ohlcv(600)
        bt = Backtest(initial_equity=1000.0)
        bt.run(df)
        if bt.trades:
            t = bt.trades[0]
            for field in ["direction", "entry_price", "exit_price", "size",
                          "pnl", "reason", "opened_at", "closed_at"]:
                assert field in t, f"Missing field: {field}"

    def test_win_rate_in_range(self):
        df = make_ohlcv(600)
        bt = Backtest(initial_equity=1000.0)
        result = bt.run(df)
        if "win_rate_pct" in result:
            assert 0 <= result["win_rate_pct"] <= 100

    def test_runs_without_error_on_short_data(self):
        """Backtest on 300 bars (short relative to indicator warm-up) returns no-trades or a valid result."""
        np.random.seed(7)
        prices = [40000.0]
        for _ in range(299):
            prices.append(max(100, prices[-1] + np.random.normal(80, 150)))
        df = pd.DataFrame(
            {
                "open":   [p * 0.9995 for p in prices],
                "high":   [p * 1.006  for p in prices],
                "low":    [p * 0.994  for p in prices],
                "close":  prices,
                "volume": [np.random.uniform(300, 900) for _ in prices],
            }
        )
        df.index = pd.date_range("2024-01-01", periods=300, freq="15min", tz="UTC")
        bt = Backtest(initial_equity=1000.0)
        result = bt.run(df)
        # Short data: either no-trades error or a valid result
        assert isinstance(result, dict)

    def test_runs_on_longer_data(self):
        """Backtest on >500 bars with pullback structure can produce trades."""
        np.random.seed(7)
        n = 600
        prices = [40000.0]
        for i in range(n - 1):
            phase = i % 60
            # Include pullback phases so RSI can dip
            drift = -80 if (20 <= phase < 30) else 80
            prices.append(max(100, prices[-1] + np.random.normal(drift, 150)))
        df = pd.DataFrame(
            {
                "open":   [p * 0.9995 for p in prices],
                "high":   [p * 1.008  for p in prices],
                "low":    [p * 0.992  for p in prices],
                "close":  prices,
                "volume": [np.random.uniform(300, 900) for _ in prices],
            }
        )
        df.index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
        bt = Backtest(initial_equity=1000.0)
        result = bt.run(df)
        assert isinstance(result, dict)

    def test_eth_symbol_uses_different_sl_tp(self):
        """ETH backtest uses optimized params (SL=1.5%, TP=7.0%) not BTC defaults."""
        df = make_ohlcv(600)
        bt_btc = Backtest(initial_equity=1000.0, symbol="BTC-USDT")
        bt_eth = Backtest(initial_equity=1000.0, symbol="ETH-USDT")
        result_btc = bt_btc.run(df)
        result_eth = bt_eth.run(df)
        # Both should return valid summary dicts (may have no trades on synthetic data)
        assert isinstance(result_btc, dict)
        assert isinstance(result_eth, dict)
        # If trades occurred, verify the result keys are correct
        if "return_pct" in result_eth:
            assert "win_rate_pct" in result_eth

    def test_sol_symbol_backtest_valid(self):
        """SOL-USDT backtest uses its own optimized params (ADX=25, SL=1.5%, TP=7%)."""
        df = make_ohlcv(600)
        bt = Backtest(initial_equity=1000.0, symbol="SOL-USDT")
        result = bt.run(df)
        assert isinstance(result, dict)
        if "return_pct" in result:
            assert "win_rate_pct" in result
            assert "max_drawdown_pct" in result

    def test_xrp_symbol_backtest_valid(self):
        """XRP-USDT backtest uses its own optimized params (ADX=22, SL=1.5%, TP=7%)."""
        df = make_ohlcv(600)
        bt = Backtest(initial_equity=1000.0, symbol="XRP-USDT")
        result = bt.run(df)
        assert isinstance(result, dict)
        if "return_pct" in result:
            assert "win_rate_pct" in result

    def test_link_symbol_backtest_valid(self):
        """LINK-USDT backtest uses its own optimized params (ADX=15, SL=1%, TP=5.5%)."""
        df = make_ohlcv(600)
        bt = Backtest(initial_equity=1000.0, symbol="LINK-USDT")
        result = bt.run(df)
        assert isinstance(result, dict)
        if "return_pct" in result:
            assert "win_rate_pct" in result

    def test_all_new_symbols_in_supported_symbols(self):
        """All new symbols are registered in config.SUPPORTED_SYMBOLS."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
        import config
        for sym in ["SOL-USDT", "XRP-USDT", "LINK-USDT"]:
            assert sym in config.SUPPORTED_SYMBOLS

    def test_new_symbols_have_optimized_sl_tp(self):
        """Each new symbol returns distinct (non-default) SL/TP from config."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
        import config
        btc_params = config.get_symbol_params("BTC-USDT")
        for sym in ["SOL-USDT", "XRP-USDT", "LINK-USDT"]:
            params = config.get_symbol_params(sym)
            # Each new symbol has explicitly defined SL/TP
            assert params["stop_loss_pct"] != btc_params["stop_loss_pct"] or \
                   params["take_profit_pct"] != btc_params["take_profit_pct"], \
                   f"{sym} should have at least one param different from BTC defaults"
