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

    def test_trading_mode_is_valid(self):
        import config
        assert config.TRADING_MODE in {"papertrading", "realtrading"}

    def test_paper_start_equity_positive(self):
        import config
        assert config.PAPER_START_EQUITY > 0
