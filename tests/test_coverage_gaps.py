"""
test_coverage_gaps.py – Tests targeting all uncovered lines in backend modules.

Covers remaining gaps to reach 100% coverage:
  - config.py: helper functions (_env_int, _env_float, _env_str, _env_bool),
               get_api_secret fallback, module-level guards
  - strategy.py: NaN indicator path in generate_signal (line 246)
  - database.py: _db rollback, migration paths, copy trading fallback
  - exchange.py: copy trading endpoints
  - bot.py: price sync loop, symbol exposure cap, leverage failure,
            exchange order rejection, copy trading real-trading close,
            position sync failure, reconciliation edge cases
  - app.py: ingress path, real-trading manual close, mode switch equity,
            _port_is_free, stop with DB-only orphan, _all_bots
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
import database as db
from bot import TradingBot, _candles_to_df, _extract_usdt_equity, _calc_pnl
from strategy import Signal, compute_indicators, generate_signal, reset_signal_state


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_db():
    """Use a temp DB for each test."""
    _tmp = tempfile.mkdtemp()
    db.DB_PATH = Path(_tmp) / "test_coverage.db"
    db.init_db()
    yield
    for p in Path(_tmp).glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
    try:
        Path(_tmp).rmdir()
    except OSError:
        pass


def _make_raw_candles(n=200, start_price=50000.0):
    """Generate raw BloFin-format candle data (newest-first, 7+ fields)."""
    candles = []
    ts = 1700000000000
    price = start_price
    for i in range(n):
        o = price
        h = price * 1.002
        l = price * 0.998
        c = price + np.random.uniform(-50, 50)
        v = np.random.uniform(100, 500)
        vc = v * c
        candles.append([str(ts), str(o), str(h), str(l), str(c), str(v), str(vc)])
        ts += 900_000
        price = c
    candles.reverse()
    return candles


def _make_paper_bot(monkeypatch, symbol="BTC-USDT"):
    monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
    monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
    return TradingBot(symbol=symbol)


def _make_real_bot(monkeypatch, symbol="BTC-USDT"):
    monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
    return TradingBot(symbol=symbol)


# ══════════════════════════════════════════════════════════════════════════════
# config.py coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestConfigHelpers:
    """Cover _env_int, _env_float, _env_str, _env_bool edge cases."""

    def test_env_int_invalid_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_BAD_INT", "not_a_number")
        assert config._env_int("TEST_BAD_INT", 42) == 42

    def test_env_float_invalid_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_BAD_FLOAT", "not_a_float")
        assert config._env_float("TEST_BAD_FLOAT", 3.14) == 3.14

    def test_env_str_strips_quotes(self, monkeypatch):
        monkeypatch.setenv("TEST_QUOTED", '"hello"')
        assert config._env_str("TEST_QUOTED", "default") == "hello"

    def test_env_str_strips_single_quotes(self, monkeypatch):
        monkeypatch.setenv("TEST_SQUOTED", "'world'")
        assert config._env_str("TEST_SQUOTED", "default") == "world"

    def test_env_str_returns_default_for_missing(self):
        # Ensure the env var doesn't exist
        os.environ.pop("TEST_MISSING_STR", None)
        assert config._env_str("TEST_MISSING_STR", "fallback") == "fallback"

    def test_env_bool_true_values(self, monkeypatch):
        for val in ("1", "true", "yes", "TRUE", "Yes"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert config._env_bool("TEST_BOOL", False) is True

    def test_env_bool_false_values(self, monkeypatch):
        for val in ("0", "false", "no", "FALSE", "No"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert config._env_bool("TEST_BOOL", True) is False

    def test_env_bool_empty_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "")
        assert config._env_bool("TEST_BOOL", True) is True
        assert config._env_bool("TEST_BOOL", False) is False

    def test_env_bool_unrecognised_returns_default(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "maybe")
        assert config._env_bool("TEST_BOOL", True) is True

    def test_get_api_secret_invalid_b64_falls_back(self, monkeypatch):
        """Invalid base64 that can't be decoded returns the raw string."""
        monkeypatch.setattr(config, "_SECRET_B64", "!!!not-valid-b64!!!")
        result = config.get_api_secret()
        assert result == "!!!not-valid-b64!!!"

    def test_get_symbol_params_unknown_symbol(self):
        params = config.get_symbol_params("UNKNOWN-USDT")
        assert "stop_loss_pct" in params
        assert "take_profit_pct" in params


class TestConfigModuleLevelGuards:
    """Test config.py module-level validation guards via importlib.reload.

    These guards run at import time. We monkeypatch env vars and reload
    the config module to trigger the guard branches.
    """

    def _reload_config(self, monkeypatch, env_overrides):
        """Reload config module with specific env var overrides."""
        import importlib
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
        # Prevent credentials file from interfering
        if "TRADING_CREDENTIALS_FILE" not in env_overrides:
            monkeypatch.setenv("TRADING_CREDENTIALS_FILE", "/nonexistent/path")
        importlib.reload(config)

    def test_max_open_positions_zero_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_OPEN_POSITIONS": "0"})
        assert config.MAX_OPEN_POSITIONS == 1

    def test_max_open_positions_negative_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_OPEN_POSITIONS": "-5"})
        assert config.MAX_OPEN_POSITIONS == 1

    def test_max_margin_usage_zero_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_MARGIN_USAGE_PCT": "0"})
        assert config.MAX_MARGIN_USAGE_PCT == pytest.approx(0.40)

    def test_max_margin_usage_over_one_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_MARGIN_USAGE_PCT": "1.5"})
        assert config.MAX_MARGIN_USAGE_PCT == pytest.approx(0.40)

    def test_max_portfolio_risk_invalid_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_PORTFOLIO_RISK_PCT": "-1"})
        assert config.MAX_PORTFOLIO_RISK_PCT == pytest.approx(0.03)

    def test_max_portfolio_risk_over_one_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_PORTFOLIO_RISK_PCT": "2.0"})
        assert config.MAX_PORTFOLIO_RISK_PCT == pytest.approx(0.03)

    def test_max_symbol_exposure_invalid_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_SYMBOL_EXPOSURE_PCT": "2.0"})
        assert config.MAX_SYMBOL_EXPOSURE_PCT == pytest.approx(0.50)

    def test_max_symbol_exposure_zero_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"MAX_SYMBOL_EXPOSURE_PCT": "0"})
        assert config.MAX_SYMBOL_EXPOSURE_PCT == pytest.approx(0.50)

    def test_margin_mode_invalid_falls_back(self, monkeypatch):
        self._reload_config(monkeypatch, {"TRADING_MARGIN_MODE": "invalid"})
        assert config.TRADING_MARGIN_MODE == "isolated"

    def test_trading_mode_invalid_resets_leverage(self, monkeypatch):
        self._reload_config(monkeypatch, {"TRADING_MODE": "badmode"})
        assert config.TRADING_MODE == "realtrading"
        assert config.LEVERAGE == 1

    def test_leverage_capped_at_125(self, monkeypatch):
        self._reload_config(monkeypatch, {"TRADING_LEVERAGE": "200", "TRADING_MODE": "papertrading"})
        assert config.LEVERAGE == 125

    def test_risk_per_trade_negative_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"RISK_PER_TRADE": "-0.5"})
        assert config.RISK_PER_TRADE == pytest.approx(0.01)

    def test_risk_per_trade_over_one_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"RISK_PER_TRADE": "2.0"})
        assert config.RISK_PER_TRADE == pytest.approx(1.0)

    def test_paper_start_equity_negative_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"PAPER_START_EQUITY": "-100"})
        assert config.PAPER_START_EQUITY == pytest.approx(1000.0)

    def test_paper_start_equity_zero_clamped(self, monkeypatch):
        self._reload_config(monkeypatch, {"PAPER_START_EQUITY": "0"})
        assert config.PAPER_START_EQUITY == pytest.approx(1000.0)

    def test_credentials_file_loading(self, monkeypatch):
        """Cover line 20: load_dotenv when credentials file exists."""
        _tmp = tempfile.mkdtemp()
        cred_file = Path(_tmp) / "test_creds.env"
        cred_file.write_text("BLOFIN_API_KEY=test_reload_key_123\n")
        self._reload_config(monkeypatch, {"TRADING_CREDENTIALS_FILE": str(cred_file)})
        assert config.BLOFIN_API_KEY == "test_reload_key_123"

    def test_env_bool_true_value(self, monkeypatch):
        self._reload_config(monkeypatch, {"COPY_TRADING_ENABLED": "true"})
        assert config.COPY_TRADING_ENABLED is True

    def test_env_bool_false_value(self, monkeypatch):
        self._reload_config(monkeypatch, {"COPY_TRADING_ENABLED": "false"})
        assert config.COPY_TRADING_ENABLED is False


class TestConfigEnvStr:
    def test_env_str_with_none_value(self, monkeypatch):
        """Cover _env_str returning default when raw is None (line 60).

        os.getenv returns None only when no default is provided.
        We monkeypatch os.getenv to return None to hit this guard.
        """
        monkeypatch.setattr(os, "getenv", lambda name, default=None: None)
        result = config._env_str("TEST_NONE_STR", "my_default")
        assert result == "my_default"


# ══════════════════════════════════════════════════════════════════════════════
# strategy.py coverage – line 246 (NaN indicator check)
# ══════════════════════════════════════════════════════════════════════════════


class TestStrategyNaNIndicators:
    def test_generate_signal_returns_none_when_indicators_nan(self):
        """NaN in required indicators returns NONE (line 246 coverage)."""
        reset_signal_state()
        n = 200
        prices = [50000.0 + np.random.uniform(-100, 100) for _ in range(n)]
        df = pd.DataFrame({
            "open": prices,
            "high": [p * 1.005 for p in prices],
            "low": [p * 0.995 for p in prices],
            "close": prices,
            "volume": [300.0] * n,
        })
        df.index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
        df = compute_indicators(df)

        # Force a NaN in a required column on the last row
        df.iloc[-1, df.columns.get_loc("rsi")] = np.nan

        result = generate_signal(df, symbol="BTC-USDT")
        assert result == Signal.NONE


# ══════════════════════════════════════════════════════════════════════════════
# database.py coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestDatabaseEdgeCoverage:
    def test_db_rollback_on_exception(self):
        """Verify _db context manager rolls back on exception (lines 38-40)."""
        with pytest.raises(RuntimeError):
            with db._db() as conn:
                conn.execute(
                    "INSERT INTO bot_logs (level, message, ts) VALUES (?, ?, ?)",
                    ("INFO", "will_rollback", "2024-01-01T00:00:00"),
                )
                raise RuntimeError("Intentional failure")

        # The insert should have been rolled back
        logs = db.get_logs(100)
        assert not any(l["message"] == "will_rollback" for l in logs)

    def test_copy_trading_config_fallback_no_row(self):
        """When the copy_trading_config row is deleted, return defaults (line 381)."""
        with db._db() as conn:
            conn.execute("DELETE FROM copy_trading_config")
        result = db.get_copy_trading_config()
        assert result == {"enabled": False, "trader_id": ""}

    def test_init_db_migration_add_columns(self):
        """Simulate a DB without signal_hint/waiting_for columns (line 140)."""
        _tmp = tempfile.mkdtemp()
        old_path = db.DB_PATH
        db.DB_PATH = Path(_tmp) / "migration_test.db"

        try:
            # Create a minimal DB without the newer columns
            import sqlite3
            conn = sqlite3.connect(str(db.DB_PATH))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    size REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    pnl REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    leverage INTEGER NOT NULL DEFAULT 5,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS bot_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_status (
                    symbol TEXT PRIMARY KEY,
                    running INTEGER NOT NULL DEFAULT 0,
                    last_signal TEXT NOT NULL DEFAULT 'NONE',
                    last_price REAL,
                    equity REAL,
                    open_trades INTEGER NOT NULL DEFAULT 0,
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    win_trades INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS copy_trading_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    trader_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO copy_trading_config (id, enabled, trader_id, updated_at)
                VALUES (1, 0, '', datetime('now'));
            """)
            conn.close()

            # Now run init_db which should add missing columns
            db.init_db()

            # Verify the new columns exist
            with db._db() as conn2:
                cols = {row[1] for row in conn2.execute("PRAGMA table_info(bot_status)").fetchall()}
            assert "signal_hint" in cols
            assert "waiting_for" in cols
            assert "long_ready" in cols
            assert "short_ready" in cols
        finally:
            db.DB_PATH = old_path

    def test_init_db_migration_old_id_schema(self):
        """Simulate old singleton schema with id column instead of symbol (line 107)."""
        _tmp = tempfile.mkdtemp()
        old_path = db.DB_PATH
        db.DB_PATH = Path(_tmp) / "old_schema.db"

        try:
            import sqlite3
            conn = sqlite3.connect(str(db.DB_PATH))
            conn.executescript("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    size REAL NOT NULL,
                    sl_price REAL,
                    tp_price REAL,
                    pnl REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    leverage INTEGER NOT NULL DEFAULT 5,
                    notes TEXT
                );
                CREATE TABLE bot_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
                CREATE TABLE bot_status (
                    id INTEGER PRIMARY KEY,
                    running INTEGER NOT NULL DEFAULT 0,
                    last_signal TEXT NOT NULL DEFAULT 'NONE',
                    last_price REAL,
                    equity REAL,
                    open_trades INTEGER NOT NULL DEFAULT 0,
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    win_trades INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE copy_trading_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    trader_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO copy_trading_config (id, enabled, trader_id, updated_at) VALUES (1, 0, '', datetime('now'));
            """)
            conn.close()

            # init_db should migrate from old id-based to symbol-based schema
            db.init_db()

            with db._db() as conn2:
                cols = {row[1] for row in conn2.execute("PRAGMA table_info(bot_status)").fetchall()}
            assert "symbol" in cols
            assert "id" not in cols
        finally:
            db.DB_PATH = old_path


# ══════════════════════════════════════════════════════════════════════════════
# exchange.py coverage – copy trading endpoints
# ══════════════════════════════════════════════════════════════════════════════


class TestExchangeCopyTrading:
    def test_get_copy_trader_positions(self, monkeypatch):
        from exchange import BloFinClient
        client = BloFinClient()
        mock_resp = {"data": [{"instId": "BTC-USDT", "side": "long", "pos": "1.5"}]}
        monkeypatch.setattr(client, "_get", lambda *a, **kw: mock_resp)
        result = client.get_copy_trader_positions("trader123")
        assert len(result) == 1
        assert result[0]["instId"] == "BTC-USDT"

    def test_get_copy_trader_positions_none_data(self, monkeypatch):
        from exchange import BloFinClient
        client = BloFinClient()
        monkeypatch.setattr(client, "_get", lambda *a, **kw: {"data": None})
        result = client.get_copy_trader_positions("trader123")
        assert result == []

    def test_get_copy_trader_order_history(self, monkeypatch):
        from exchange import BloFinClient
        client = BloFinClient()
        mock_resp = {"data": [{"instId": "BTC-USDT", "side": "buy"}]}
        monkeypatch.setattr(client, "_get", lambda *a, **kw: mock_resp)
        result = client.get_copy_trader_order_history("trader123", limit=10)
        assert len(result) == 1

    def test_get_copy_trader_order_history_none_data(self, monkeypatch):
        from exchange import BloFinClient
        client = BloFinClient()
        monkeypatch.setattr(client, "_get", lambda *a, **kw: {"data": None})
        result = client.get_copy_trader_order_history("trader123")
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# bot.py coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestBotPriceSyncLoop:
    def test_price_sync_updates_price_and_equity(self, monkeypatch):
        """Cover _price_sync_loop inner body (lines 172-189)."""
        bot = _make_paper_bot(monkeypatch)
        # Make _stop_event.wait return True immediately on second call to exit loop
        call_count = [0]
        original_wait = bot._stop_event.wait

        def fake_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First wait (the 30s stagger) – skip it
                return False
            # Second wait (the price sync loop) – exit
            return True

        monkeypatch.setattr(bot._stop_event, "wait", fake_wait)
        bot._running = True

        # Mock ticker response
        monkeypatch.setattr(
            bot._client, "get_ticker",
            lambda sym: {"last": "51000.50"}
        )

        bot._price_sync_loop()

        status = db.get_bot_status("BTC-USDT")
        assert status["last_price"] == pytest.approx(51000.50, abs=1)

    def test_price_sync_handles_error(self, monkeypatch):
        """Cover price sync exception handler (line 186)."""
        bot = _make_paper_bot(monkeypatch)
        call_count = [0]

        def fake_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return False
            return True

        monkeypatch.setattr(bot._stop_event, "wait", fake_wait)
        bot._running = True

        monkeypatch.setattr(
            bot._client, "get_ticker",
            MagicMock(side_effect=ConnectionError("network down"))
        )

        bot._price_sync_loop()  # should not raise

    def test_price_sync_stops_on_initial_event(self, monkeypatch):
        """If stop event is set before first iteration, loop exits (line 170)."""
        bot = _make_paper_bot(monkeypatch)
        monkeypatch.setattr(bot._stop_event, "wait", lambda timeout=None: True)
        bot._running = True
        bot._price_sync_loop()  # should exit immediately

    def test_price_sync_no_raw_price(self, monkeypatch):
        """Cover case where ticker returns no price field."""
        bot = _make_paper_bot(monkeypatch)
        call_count = [0]

        def fake_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return False
            return True

        monkeypatch.setattr(bot._stop_event, "wait", fake_wait)
        bot._running = True
        monkeypatch.setattr(bot._client, "get_ticker", lambda sym: {})

        bot._price_sync_loop()  # should not raise

    def test_price_sync_lastPr_field(self, monkeypatch):
        """Cover ticker with 'lastPr' field instead of 'last'."""
        bot = _make_paper_bot(monkeypatch)
        call_count = [0]

        def fake_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return False
            return True

        monkeypatch.setattr(bot._stop_event, "wait", fake_wait)
        bot._running = True
        monkeypatch.setattr(
            bot._client, "get_ticker",
            lambda sym: {"lastPr": "42000.0"}
        )

        bot._price_sync_loop()

        status = db.get_bot_status("BTC-USDT")
        assert status["last_price"] == pytest.approx(42000.0, abs=1)


class TestBotSymbolExposureCap:
    def test_blocks_symbol_exposure_cap(self, monkeypatch):
        """Cover portfolio cap #4: per-symbol notional (lines 404-410)."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 100000.0)
        monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 10)
        monkeypatch.setattr(config, "MAX_MARGIN_USAGE_PCT", 0.99)    # high – won't block
        monkeypatch.setattr(config, "MAX_PORTFOLIO_RISK_PCT", 0.99)  # high – won't block
        monkeypatch.setattr(config, "MAX_SYMBOL_EXPOSURE_PCT", 0.001)  # very low
        bot = TradingBot(symbol="BTC-USDT")

        # Open a small trade on a different symbol so we pass position count
        db.open_trade("ETH-USDT", "LONG", 2000.0, 0.01, 1900.0, 2200.0, 5)

        # The new trade for BTC-USDT should be blocked by symbol exposure cap
        assert bot._portfolio_allows_entry(100000.0, 50000.0) is False


class TestBotRealTradingEnter:
    def test_set_leverage_failure(self, monkeypatch):
        """Cover set_leverage failure path (lines 445-446)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "set_leverage",
            MagicMock(side_effect=RuntimeError("leverage failed"))
        )
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(return_value={"code": "0", "data": [{"orderId": "123"}]})
        )

        bot._enter_trade(Signal.LONG, 50000.0, 10000.0)

        # Verify the trade was opened despite leverage failure
        trades = db.get_open_trades("BTC-USDT")
        assert len(trades) == 1

    def test_exchange_order_rejection_code(self, monkeypatch):
        """Cover exchange order rejection code != 0 (line 464)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "set_leverage",
            MagicMock(return_value={"code": "0"})
        )
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(return_value={"code": "51000", "msg": "Insufficient balance"})
        )

        bot._enter_trade(Signal.LONG, 50000.0, 10000.0)

        # Trade should NOT be opened
        trades = db.get_open_trades("BTC-USDT")
        assert len(trades) == 0

    def test_place_order_exception(self, monkeypatch):
        """Cover place_order raising exception (line 474)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "set_leverage",
            MagicMock(return_value={"code": "0"})
        )
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(side_effect=Exception("connection timeout"))
        )

        bot._enter_trade(Signal.LONG, 50000.0, 10000.0)

        trades = db.get_open_trades("BTC-USDT")
        assert len(trades) == 0


class TestBotHasExchangePosition:
    def test_position_sync_failure(self, monkeypatch):
        """Cover _has_exchange_open_position exception (lines 646-648)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "get_positions",
            MagicMock(side_effect=RuntimeError("API down"))
        )

        result = bot._has_exchange_open_position()
        assert result is False

    def test_position_with_size_field(self, monkeypatch):
        """Cover position dict using 'size' key instead of 'positions'."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "get_positions",
            MagicMock(return_value=[{"size": "0.5"}])
        )

        result = bot._has_exchange_open_position()
        assert result is True

    def test_position_with_unparseable_size(self, monkeypatch):
        """Cover position with non-numeric size (lines 655-656)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "get_positions",
            MagicMock(return_value=[{"positions": "invalid"}])
        )

        result = bot._has_exchange_open_position()
        assert result is False

    def test_position_none_positions(self, monkeypatch):
        """Cover when get_positions returns None."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "get_positions",
            MagicMock(return_value=None)
        )

        result = bot._has_exchange_open_position()
        assert result is False


class TestBotReconciliation:
    def test_exchange_pos_no_local_trade(self, monkeypatch):
        """Cover reconciliation: exchange has pos but no local trade (line 678)."""
        bot = _make_real_bot(monkeypatch)
        result = bot._reconcile_local_open_trades([], True, 50000.0)
        assert result == []


class TestBotCopyTradingRealMode:
    def test_copy_close_real_trading_success(self, monkeypatch):
        """Cover real-trading copy close order path (lines 565-597)."""
        bot = _make_real_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        # Set up known positions (had a LONG before)
        bot._known_copy_positions = {Signal.LONG}

        # Create an open trade
        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")

        # Remote positions now empty (lead trader closed)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=[])
        )
        # Mock successful close order
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(return_value={"code": "0"})
        )
        # Mock balance for equity refresh
        monkeypatch.setattr(
            bot._client, "get_balance",
            MagicMock(return_value={"details": [{"currency": "USDT", "equity": "10000"}]})
        )

        bot._tick_copy_trading(open_trades, False, 51000.0, 10000.0)

        # Trade should be closed
        closed = db.get_trade_by_id(trade_id)
        assert closed["status"] == "CLOSED"

    def test_copy_close_real_trading_rejected(self, monkeypatch):
        """Cover copy close order rejection (lines 585-591)."""
        bot = _make_real_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        bot._known_copy_positions = {Signal.LONG}

        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")

        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=[])
        )
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(return_value={"code": "51000", "msg": "rejected"})
        )

        bot._tick_copy_trading(open_trades, False, 51000.0, 10000.0)

        # Trade should still be open (close was rejected)
        trade = db.get_trade_by_id(trade_id)
        assert trade["status"] == "OPEN"

    def test_copy_close_real_trading_exception(self, monkeypatch):
        """Cover copy close order exception (lines 592-597)."""
        bot = _make_real_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        bot._known_copy_positions = {Signal.SHORT}

        trade_id = db.open_trade("BTC-USDT", "SHORT", 50000.0, 0.1, 52000.0, 45000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")

        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=[])
        )
        monkeypatch.setattr(
            bot._client, "place_order",
            MagicMock(side_effect=ConnectionError("network error"))
        )

        bot._tick_copy_trading(open_trades, False, 49000.0, 10000.0)

        # Trade should still be open (close failed)
        trade = db.get_trade_by_id(trade_id)
        assert trade["status"] == "OPEN"

    def test_copy_trading_side_parsing(self, monkeypatch):
        """Cover side parsing for buy/sell/net_long/net_short (line 526)."""
        bot = _make_paper_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._known_copy_positions = set()
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        # Remote has various side formats
        positions = [
            {"instId": "BTC-USDT", "side": "buy", "pos": "0.5"},
            {"instId": "BTC-USDT", "side": "net_long", "pos": "0.5"},
            {"instId": "BTC-USDT", "side": "unknown_side", "pos": "0.5"},
        ]
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=positions)
        )

        bot._tick_copy_trading([], False, 50000.0, 10000.0)

        # Should have detected LONG direction
        assert Signal.LONG in bot._known_copy_positions

    def test_copy_trading_zero_size_ignored(self, monkeypatch):
        """Cover copy trading non-zero size check (lines 532-533)."""
        bot = _make_paper_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._known_copy_positions = set()

        positions = [
            {"instId": "BTC-USDT", "side": "long", "pos": "0"},
            {"instId": "BTC-USDT", "side": "short", "pos": "0.0"},
        ]
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=positions)
        )

        bot._tick_copy_trading([], False, 50000.0, 10000.0)

        # Zero-size positions should be ignored
        assert len(bot._known_copy_positions) == 0

    def test_copy_trading_invalid_size_type_error(self, monkeypatch):
        """Cover copy trading TypeError/ValueError in size parsing (lines 532-533)."""
        bot = _make_paper_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._known_copy_positions = set()

        # Use values that will trigger float() ValueError
        positions = [
            {"instId": "BTC-USDT", "side": "long", "pos": "not_a_number"},
            {"instId": "BTC-USDT", "side": "short", "size": "invalid"},
        ]
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=positions)
        )

        bot._tick_copy_trading([], False, 50000.0, 10000.0)

        # Invalid sizes should be ignored
        assert len(bot._known_copy_positions) == 0

    def test_copy_trading_skip_entry_when_existing_trade(self, monkeypatch):
        """Cover copy trading skip-entry when trade already exists."""
        bot = _make_paper_bot(monkeypatch)
        bot._copy_trading = True
        bot._copy_trader_id = "trader123"
        bot._known_copy_positions = set()

        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")

        positions = [
            {"instId": "BTC-USDT", "side": "short", "pos": "1.0"},
        ]
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            MagicMock(return_value=positions)
        )

        bot._tick_copy_trading(open_trades, False, 50000.0, 10000.0)

        # Original trade should still be there, no new short entered
        trades = db.get_open_trades("BTC-USDT")
        assert len(trades) == 1
        assert trades[0]["direction"] == "LONG"


class TestBotTickRealTrading:
    def test_tick_balance_failure(self, monkeypatch):
        """Cover get_balance failure in _tick (lines 233-234)."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)
        monkeypatch.setattr(bot._client, "get_balance", MagicMock(side_effect=RuntimeError("API down")))
        monkeypatch.setattr(bot._client, "get_positions", MagicMock(return_value=[]))

        bot._tick()  # should not raise


class TestBotStopJoins:
    def test_stop_joins_price_thread(self, monkeypatch):
        """Cover _price_thread.join path (line 130)."""
        bot = _make_paper_bot(monkeypatch)
        # Simulate threads that are alive
        bot._thread = MagicMock()
        bot._thread.is_alive.return_value = True
        bot._price_thread = MagicMock()
        bot._price_thread.is_alive.return_value = True
        bot._running = True

        bot.stop()

        bot._thread.join.assert_called_once_with(timeout=2)
        bot._price_thread.join.assert_called_once_with(timeout=2)
        assert not bot._running


class TestBotRefreshEquity:
    def test_refresh_equity_real_failure(self, monkeypatch):
        """Cover _refresh_equity_after_close failure for real trading."""
        bot = _make_real_bot(monkeypatch)
        bot._stop_event = MagicMock()
        bot._stop_event.wait = MagicMock(return_value=False)

        monkeypatch.setattr(
            bot._client, "get_balance",
            MagicMock(side_effect=RuntimeError("API down"))
        )

        bot._refresh_equity_after_close()  # should not raise


# ══════════════════════════════════════════════════════════════════════════════
# app.py coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestAppCoverage:
    @pytest.fixture()
    def app_client(self, tmp_path):
        """Flask test client with isolated DB and clean bot registry."""
        old_dir, old_stem = db._DB_DIR, db._DB_STEM
        db._DB_DIR = tmp_path
        db._DB_STEM = "test_coverage_app"
        db.DB_PATH = tmp_path / "test_coverage_app.db"
        db.init_db()

        import app as flask_app
        flask_app._bots.clear()
        flask_app.app.config["TESTING"] = True

        with flask_app.app.test_client() as client:
            yield client, flask_app

        flask_app._bots.clear()
        db._DB_DIR, db._DB_STEM = old_dir, old_stem

    def test_ingress_path_injection(self, app_client, tmp_path):
        """Cover ingress path injection for Home Assistant (lines 91-97)."""
        client, flask_app = app_client
        # Create a minimal index.html in the frontend dir
        frontend_dir = flask_app._FRONTEND_DIR
        os.makedirs(frontend_dir, exist_ok=True)
        index_path = os.path.join(frontend_dir, "index.html")
        if not os.path.exists(index_path):
            with open(index_path, "w") as f:
                f.write("<html><head></head><body>test</body></html>")

        resp = client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123/"})
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'ingress-path' in html
        assert '/api/hassio_ingress/abc123' in html

    def test_all_bots_helper(self, app_client):
        """Cover _all_bots function (line 125)."""
        _, flask_app = app_client
        bots = flask_app._all_bots()
        assert len(bots) == len(config.SUPPORTED_SYMBOLS)

    def test_get_bot_creates_new_instance(self, app_client):
        """Cover _get_bot lazy creation (line 120)."""
        _, flask_app = app_client
        flask_app._bots.clear()
        bot = flask_app._get_bot("BTC-USDT")
        assert bot is not None
        assert "BTC-USDT" in flask_app._bots

    def test_manual_close_trade_real_trading(self, app_client, monkeypatch):
        """Cover real-trading close path in api_close_trade (lines 200-209)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        # Create an open trade and set last_price
        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        db.update_bot_status(symbol="BTC-USDT", last_price=51000.0)

        # Mock the BloFinClient.place_order
        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "place_order",
            MagicMock(return_value={"code": "0"})
        )

        resp = client.post(f"/api/trades/{trade_id}/close")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_manual_close_trade_exchange_rejection(self, app_client, monkeypatch):
        """Cover exchange rejection in manual close (lines 206-207)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        trade_id = db.open_trade("BTC-USDT", "SHORT", 50000.0, 0.1, 52000.0, 45000.0, 5)
        db.update_bot_status(symbol="BTC-USDT", last_price=49000.0)

        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "place_order",
            MagicMock(return_value={"code": "51000", "msg": "Insufficient balance"})
        )

        resp = client.post(f"/api/trades/{trade_id}/close")
        assert resp.status_code == 502

    def test_manual_close_trade_exchange_error(self, app_client, monkeypatch):
        """Cover exchange exception in manual close (lines 208-209)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        db.update_bot_status(symbol="BTC-USDT", last_price=51000.0)

        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "place_order",
            MagicMock(side_effect=ConnectionError("network down"))
        )

        resp = client.post(f"/api/trades/{trade_id}/close")
        assert resp.status_code == 502

    def test_manual_close_refreshes_equity_live(self, app_client, monkeypatch):
        """Cover live equity refresh after manual close (lines 231-235)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        db.update_bot_status(symbol="BTC-USDT", last_price=51000.0)

        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "place_order",
            MagicMock(return_value={"code": "0"})
        )

        # Create and start a mock bot for equity refresh
        mock_bot = MagicMock()
        mock_bot.is_running = True
        flask_app._bots["BTC-USDT"] = mock_bot

        resp = client.post(f"/api/trades/{trade_id}/close")
        assert resp.status_code == 200
        mock_bot._refresh_equity_after_close.assert_called_once()

    def test_stop_with_db_orphaned_flag(self, app_client, monkeypatch):
        """Cover stop with DB-only running flag but no in-memory bot (lines 331-332)."""
        client, flask_app = app_client

        # Set a running flag in DB but no bot in memory
        db.update_bot_status(symbol="BTC-USDT", running=1)

        resp = client.post("/api/bot/stop")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        status = db.get_bot_status("BTC-USDT")
        assert status["running"] == 0

    def test_mode_switch_real_trading_equity_refresh(self, app_client, monkeypatch):
        """Cover real-trading equity refresh on mode switch (lines 351-352)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")

        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "get_balance",
            MagicMock(return_value={"details": [{"currency": "USDT", "equity": "5000"}]})
        )

        resp = client.post(
            "/api/trading/mode",
            json={"mode": "realtrading"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["mode"] == "realtrading"

    def test_mode_switch_real_balance_failure(self, app_client, monkeypatch):
        """Cover real-trading balance failure on mode switch (lines 367, 372-373)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")

        from exchange import BloFinClient
        monkeypatch.setattr(
            BloFinClient, "get_balance",
            MagicMock(side_effect=ConnectionError("cannot connect"))
        )

        resp = client.post(
            "/api/trading/mode",
            json={"mode": "realtrading"},
            content_type="application/json",
        )
        assert resp.status_code == 200

        # When balance fetch fails, equity should be set to None for all symbols
        for sym in config.SUPPORTED_SYMBOLS:
            status = db.get_bot_status(sym)
            assert status.get("equity") is None

    def test_port_is_free(self):
        """Cover _port_is_free function (lines 582-584)."""
        import app as flask_app
        # Test with a port that's definitely not in use
        assert flask_app._port_is_free(59999) is True

    def test_port_is_free_used_port(self):
        """Cover _port_is_free with a port that is in use."""
        import socket
        import app as flask_app

        # Bind a port temporarily
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 59998))
        sock.listen(1)

        try:
            assert flask_app._port_is_free(59998) is False
        finally:
            sock.close()


class TestAppModeSwitch:
    @pytest.fixture()
    def app_client(self, tmp_path):
        old_dir, old_stem = db._DB_DIR, db._DB_STEM
        db._DB_DIR = tmp_path
        db._DB_STEM = "test_mode"
        db.DB_PATH = tmp_path / "test_mode.db"
        db.init_db()

        import app as flask_app
        import config as _config
        flask_app._bots.clear()
        flask_app.app.config["TESTING"] = True

        old_copy = _config.COPY_TRADING_ENABLED

        with flask_app.app.test_client() as client:
            yield client, flask_app

        flask_app._bots.clear()
        db._DB_DIR, db._DB_STEM = old_dir, old_stem
        _config.COPY_TRADING_ENABLED = old_copy

    def test_mode_switch_equity_update_exception(self, app_client, monkeypatch):
        """Cover per-symbol equity update exception during mode switch."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        # Make db.update_bot_status raise for the per-symbol path so the
        # exception handler inside the loop is exercised.
        original_update = db.update_bot_status
        call_count = {"n": 0}

        def failing_update(symbol, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("DB error on purpose")
            return original_update(symbol, **kwargs)

        monkeypatch.setattr(db, "update_bot_status", failing_update)

        # Switch to paper trading (which calls update_bot_status for each symbol)
        resp = client.post(
            "/api/trading/mode",
            json={"mode": "papertrading"},
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_manual_close_equity_refresh_exception(self, app_client, monkeypatch):
        """Cover equity refresh exception after manual close (lines 235-236)."""
        client, flask_app = app_client
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")

        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 55000.0, 5)
        db.update_bot_status(symbol="BTC-USDT", last_price=51000.0)

        # Make get_trade_stats raise to trigger the except block
        monkeypatch.setattr(db, "get_trade_stats", MagicMock(side_effect=RuntimeError("DB error")))

        resp = client.post(f"/api/trades/{trade_id}/close")
        # Should still succeed – the exception is swallowed
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
