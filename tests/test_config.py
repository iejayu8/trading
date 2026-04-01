"""
Tests for config module (credential handling).
"""

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


class TestConfig:
    def test_get_api_secret_decodes_b64(self, monkeypatch):
        raw_secret = "my_super_secret_key"
        encoded    = base64.b64encode(raw_secret.encode()).decode()
        monkeypatch.setenv("BLOFIN_API_SECRET_B64", encoded)

        # Re-import config with patched env
        import importlib
        import config
        monkeypatch.setattr(config, "_SECRET_B64", encoded)
        decoded = config.get_api_secret()
        assert decoded == raw_secret

    def test_get_api_secret_empty(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "_SECRET_B64", "")
        assert config.get_api_secret() == ""

    def test_default_trading_params(self):
        import config
        assert config.LEVERAGE == 5
        assert 0 < config.RISK_PER_TRADE <= 0.05
        assert config.STOP_LOSS_PCT < config.TAKE_PROFIT_PCT
        assert config.FAST_EMA < config.SLOW_EMA < config.TREND_EMA

    def test_supported_symbols_contains_btc(self):
        import config
        assert any("BTC" in s for s in config.SUPPORTED_SYMBOLS)

    def test_supported_symbols_contains_new_symbols(self):
        import config
        for sym in ["SOL-USDT", "XRP-USDT", "LINK-USDT"]:
            assert sym in config.SUPPORTED_SYMBOLS, f"{sym} missing from SUPPORTED_SYMBOLS"

    def test_get_symbol_params_returns_sl_tp_for_all_symbols(self):
        import config
        for sym in config.SUPPORTED_SYMBOLS:
            params = config.get_symbol_params(sym)
            assert "stop_loss_pct" in params, f"{sym} missing stop_loss_pct"
            assert "take_profit_pct" in params, f"{sym} missing take_profit_pct"
            assert 0 < params["stop_loss_pct"] < 0.20, f"{sym} stop_loss_pct out of range"
            assert 0 < params["take_profit_pct"] < 0.50, f"{sym} take_profit_pct out of range"

    def test_new_symbols_have_custom_strategy_params(self):
        """SOL, XRP, and LINK each declare full per-symbol strategy overrides."""
        import config
        for sym, expected_keys in [
            ("SOL-USDT",  ["adx_min", "rsi_pullback_max", "rsi_recovery_long",
                            "pullback_lookback", "signal_cooldown"]),
            ("XRP-USDT",  ["adx_min", "rsi_pullback_max", "rsi_recovery_long",
                            "pullback_lookback", "signal_cooldown"]),
            ("LINK-USDT", ["adx_min", "rsi_pullback_max", "rsi_recovery_long",
                            "pullback_lookback", "signal_cooldown"]),
        ]:
            params = config.get_symbol_params(sym)
            for key in expected_keys:
                assert key in params, f"{sym} missing '{key}' in SYMBOL_PARAMS"

    def test_link_has_lower_adx_than_btc(self):
        """LINK uses a lower ADX gate (15) suited to its lower-ADX trend environment."""
        import config
        from strategy import ADX_MIN as DEFAULT_ADX
        link_params = config.get_symbol_params("LINK-USDT")
        assert link_params["adx_min"] < DEFAULT_ADX

    def test_sol_has_higher_adx_than_btc(self):
        """SOL uses a higher ADX gate (25) to filter its noisier price action."""
        import config
        from strategy import ADX_MIN as DEFAULT_ADX
        sol_params = config.get_symbol_params("SOL-USDT")
        assert sol_params["adx_min"] > DEFAULT_ADX

    def test_trading_mode_is_valid(self):
        import config
        assert config.TRADING_MODE in {"papertrading", "realtrading"}

    def test_paper_start_equity_positive(self):
        import config
        assert config.PAPER_START_EQUITY > 0
