"""
test_copy_trading.py – Tests for the copy trading feature.

Covers:
  - DB helpers: get_copy_trading_config / set_copy_trading_config
  - DB migration: copy_trading_config table is created by init_db()
  - API endpoints: GET /api/copytrading/config, POST /api/copytrading/config
  - TradingBot._tick_copy_trading(): new position opens a trade
  - TradingBot._tick_copy_trading(): closed position closes our trade
  - TradingBot._tick_copy_trading(): paper mode uses db.open_trade without place_order
  - TradingBot._tick(): reads DB config each tick and branches to copy trading
  - TradingBot.start(): loads copy trading config from DB
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
import database as db
from bot import TradingBot
from strategy import Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolate_db():
    """Use a fresh temp DB for each test."""
    _tmp = tempfile.mkdtemp()
    db.DB_PATH = Path(_tmp) / "test_copy_trading.db"
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


# ── DB helpers ────────────────────────────────────────────────────────────────

class TestCopyTradingDB:
    def test_init_db_creates_copy_trading_config_table(self):
        with db._db() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "copy_trading_config" in tables

    def test_get_copy_trading_config_defaults(self):
        cfg = db.get_copy_trading_config()
        assert cfg["enabled"] is False
        assert cfg["trader_id"] == ""

    def test_set_copy_trading_config_enable(self):
        db.set_copy_trading_config(enabled=True, trader_id="trader123")
        cfg = db.get_copy_trading_config()
        assert cfg["enabled"] is True
        assert cfg["trader_id"] == "trader123"

    def test_set_copy_trading_config_disable(self):
        db.set_copy_trading_config(enabled=True, trader_id="trader123")
        db.set_copy_trading_config(enabled=False, trader_id="")
        cfg = db.get_copy_trading_config()
        assert cfg["enabled"] is False

    def test_set_copy_trading_config_overwrites(self):
        db.set_copy_trading_config(enabled=True, trader_id="first")
        db.set_copy_trading_config(enabled=True, trader_id="second")
        cfg = db.get_copy_trading_config()
        assert cfg["trader_id"] == "second"

    def test_set_copy_trading_config_idempotent(self):
        for _ in range(3):
            db.set_copy_trading_config(enabled=True, trader_id="abc")
        cfg = db.get_copy_trading_config()
        assert cfg["trader_id"] == "abc"


# ── API endpoints ─────────────────────────────────────────────────────────────

@pytest.fixture()
def app_client(tmp_path):
    """Flask test client with isolated DB."""
    old_dir, old_stem = db._DB_DIR, db._DB_STEM
    db._DB_DIR = tmp_path
    db._DB_STEM = "test_api_copy"
    db.DB_PATH = tmp_path / "test_api_copy.db"
    db.init_db()

    import app as flask_app
    import config as _config
    flask_app._bots.clear()
    flask_app.app.config["TESTING"] = True

    old_copy = _config.COPY_TRADING_ENABLED

    with flask_app.app.test_client() as client:
        yield client

    flask_app._bots.clear()
    db._DB_DIR, db._DB_STEM = old_dir, old_stem
    _config.COPY_TRADING_ENABLED = old_copy


class TestCopyTradingAPI:
    def test_get_config_returns_defaults(self, app_client):
        resp = app_client.get("/api/copytrading/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["enabled"] is False
        assert data["trader_id"] == ""

    def test_post_config_enables_copy_trading(self, app_client):
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "mytrader"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["enabled"] is True
        assert data["trader_id"] == "mytrader"

    def test_post_config_persists_to_db(self, app_client):
        app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "mytrader"},
        )
        resp = app_client.get("/api/copytrading/config")
        data = resp.get_json()
        assert data["enabled"] is True
        assert data["trader_id"] == "mytrader"

    def test_post_config_disables_copy_trading(self, app_client):
        app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "mytrader"},
        )
        app_client.post(
            "/api/copytrading/config",
            json={"enabled": False, "trader_id": ""},
        )
        data = app_client.get("/api/copytrading/config").get_json()
        assert data["enabled"] is False

    def test_post_config_rejects_enabled_without_trader_id(self, app_client):
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": ""},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "trader_id" in data["message"].lower()

    def test_post_config_rejects_enabled_with_whitespace_only_id(self, app_client):
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "   "},
        )
        assert resp.status_code == 400

    def test_post_config_empty_body_disables(self, app_client):
        resp = app_client.post("/api/copytrading/config", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is False


# ── Bot: _tick_copy_trading ───────────────────────────────────────────────────

class TestTickCopyTrading:
    """Unit tests for TradingBot._tick_copy_trading()."""

    def _make_bot(self, monkeypatch, paper=True):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading" if paper else "realtrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")
        # Seed DB config
        db.set_copy_trading_config(enabled=True, trader_id="leader1")
        bot._copy_trading = True
        bot._copy_trader_id = "leader1"
        return bot

    def test_new_long_position_opens_trade(self, monkeypatch):
        bot = self._make_bot(monkeypatch)
        # Lead trader has a LONG BTC-USDT position
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "long", "pos": "0.1"}],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1
        assert open_trades[0]["direction"] == "LONG"

    def test_new_short_position_opens_trade(self, monkeypatch):
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "short", "pos": "0.1"}],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1
        assert open_trades[0]["direction"] == "SHORT"

    def test_position_closed_by_lead_closes_our_trade(self, monkeypatch):
        bot = self._make_bot(monkeypatch)
        # We have an open LONG trade
        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 52000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")
        # Bot previously knew about this LONG position
        bot._known_copy_positions = {"LONG"}
        # Lead trader no longer has any BTC-USDT position
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [],
        )
        bot._tick_copy_trading(
            open_trades=open_trades, exchange_has_pos=False,
            last_price=51000.0, equity=10000.0,
        )
        open_trades_after = db.get_open_trades("BTC-USDT")
        assert len(open_trades_after) == 0

    def test_paper_mode_no_place_order(self, monkeypatch):
        """In paper mode, _tick_copy_trading must not call place_order."""
        bot = self._make_bot(monkeypatch, paper=True)
        place_order_called = {"n": 0}

        def fake_place_order(*args, **kwargs):
            place_order_called["n"] += 1
            return {"code": "0"}

        monkeypatch.setattr(bot._client, "place_order", fake_place_order)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "long", "pos": "0.1"}],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        assert place_order_called["n"] == 0
        assert len(db.get_open_trades("BTC-USDT")) == 1

    def test_skips_other_symbols(self, monkeypatch):
        """Positions for other symbols are ignored."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "ETH-USDT", "side": "long", "pos": "1.0"}],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        assert len(db.get_open_trades("BTC-USDT")) == 0

    def test_skips_entry_when_trade_already_open(self, monkeypatch):
        """No duplicate entry when a trade is already open for that direction."""
        bot = self._make_bot(monkeypatch)
        db.open_trade("BTC-USDT", "LONG", 50000.0, 0.1, 48000.0, 52000.0, 5)
        open_trades = db.get_open_trades("BTC-USDT")
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "long", "pos": "0.1"}],
        )
        bot._tick_copy_trading(
            open_trades=open_trades, exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        # Still just one trade
        assert len(db.get_open_trades("BTC-USDT")) == 1

    def test_api_failure_is_logged_gracefully(self, monkeypatch):
        """Exchange errors are caught and logged; no crash."""
        bot = self._make_bot(monkeypatch)

        def explode(tid):
            raise RuntimeError("Network error")

        monkeypatch.setattr(bot._client, "get_copy_trader_positions", explode)
        # Should not raise
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        logs = db.get_logs(20)
        messages = [l["message"] for l in logs]
        assert any("COPY" in m for m in messages)

    def test_zero_size_position_ignored(self, monkeypatch):
        """A position with size=0 should not trigger an entry."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "long", "pos": "0"}],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        assert len(db.get_open_trades("BTC-USDT")) == 0

    def test_updates_bot_status_hint(self, monkeypatch):
        """signal_hint and waiting_for are updated to copy trading values."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [],
        )
        bot._tick_copy_trading(
            open_trades=[], exchange_has_pos=False,
            last_price=50000.0, equity=10000.0,
        )
        status = db.get_bot_status("BTC-USDT")
        assert status["signal_hint"] == "COPY_ACTIVE"
        assert "leader1" in status["waiting_for"]


# ── Bot: _tick branches to copy trading ──────────────────────────────────────

class TestTickBranchesToCopyTrading:
    def test_tick_uses_copy_trading_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        # Enable copy trading in DB
        db.set_copy_trading_config(enabled=True, trader_id="leader1")

        copy_called = {"n": 0}
        orig_copy_tick = bot._tick_copy_trading

        def fake_copy_tick(*args, **kwargs):
            copy_called["n"] += 1

        monkeypatch.setattr(bot, "_tick_copy_trading", fake_copy_tick)

        import bot as bot_module
        generate_signal_called = {"n": 0}
        orig_gen = bot_module.generate_signal

        def track_gen(*a, **kw):
            generate_signal_called["n"] += 1
            return orig_gen(*a, **kw)

        monkeypatch.setattr(bot_module, "generate_signal", track_gen)

        bot._tick()

        assert copy_called["n"] == 1
        assert generate_signal_called["n"] == 0

    def test_tick_uses_strategy_when_copy_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        db.set_copy_trading_config(enabled=False, trader_id="")

        import bot as bot_module
        generate_signal_called = {"n": 0}
        orig_gen = bot_module.generate_signal

        def track_gen(*a, **kw):
            generate_signal_called["n"] += 1
            return Signal.NONE

        monkeypatch.setattr(bot_module, "generate_signal", track_gen)

        bot._tick()

        assert generate_signal_called["n"] == 1


# ── Bot.start() loads copy trading config ─────────────────────────────────────

class TestBotStartLoadsCopyConfig:
    def test_start_loads_enabled_config(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        db.set_copy_trading_config(enabled=True, trader_id="mytrader")

        bot = TradingBot(symbol="BTC-USDT")
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: [])
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})

        bot.start()
        assert bot._copy_trading is True
        assert bot._copy_trader_id == "mytrader"
        bot.stop()

    def test_start_loads_disabled_config(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        db.set_copy_trading_config(enabled=False, trader_id="")

        bot = TradingBot(symbol="BTC-USDT")
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: [])
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})

        bot.start()
        assert bot._copy_trading is False
        bot.stop()


# ── COPY_SYNC_SECONDS constant ───────────────────────────────────────────────

class TestCopySyncConstant:
    def test_copy_sync_seconds_is_5(self):
        assert TradingBot.COPY_SYNC_SECONDS == 5


# ── _tick_copy_only ──────────────────────────────────────────────────────────

class TestTickCopyOnly:
    """Unit tests for the lightweight _tick_copy_only() method."""

    def _make_bot(self, monkeypatch, paper=True):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading" if paper else "realtrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")
        db.set_copy_trading_config(enabled=True, trader_id="leader1")
        bot._copy_trading = True
        bot._copy_trader_id = "leader1"
        return bot

    def test_calls_tick_copy_trading(self, monkeypatch):
        """_tick_copy_only fetches price via ticker and calls _tick_copy_trading."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})

        copy_calls = {"n": 0}

        def fake_copy_tick(*args, **kwargs):
            copy_calls["n"] += 1

        monkeypatch.setattr(bot, "_tick_copy_trading", fake_copy_tick)
        bot._tick_copy_only()

        assert copy_calls["n"] == 1
        status = db.get_bot_status("BTC-USDT")
        assert float(status["last_price"]) == 50000.0

    def test_updates_equity_paper(self, monkeypatch):
        """Paper equity is refreshed during the copy-only tick."""
        bot = self._make_bot(monkeypatch, paper=True)
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions", lambda tid: [],
        )
        bot._tick_copy_only()
        status = db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None
        assert float(status["equity"]) == 10000.0

    def test_returns_early_on_ticker_failure(self, monkeypatch):
        """If the ticker API fails, _tick_copy_only returns without crashing."""
        bot = self._make_bot(monkeypatch)

        def exploding_ticker(s):
            raise RuntimeError("Network error")

        monkeypatch.setattr(bot._client, "get_ticker", exploding_ticker)

        copy_calls = {"n": 0}
        monkeypatch.setattr(bot, "_tick_copy_trading", lambda *a, **k: copy_calls.update(n=copy_calls["n"] + 1))
        bot._tick_copy_only()
        assert copy_calls["n"] == 0  # not reached

    def test_returns_early_on_no_price(self, monkeypatch):
        """If ticker returns no price, _tick_copy_only logs a warning and returns."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {})

        copy_calls = {"n": 0}
        monkeypatch.setattr(bot, "_tick_copy_trading", lambda *a, **k: copy_calls.update(n=copy_calls["n"] + 1))
        bot._tick_copy_only()
        assert copy_calls["n"] == 0

    def test_skips_copy_if_disabled_at_runtime(self, monkeypatch):
        """If copy trading is disabled between loop iterations, skip mirroring."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})
        # Disable copy trading in DB after bot was created
        db.set_copy_trading_config(enabled=False, trader_id="")

        copy_calls = {"n": 0}
        monkeypatch.setattr(bot, "_tick_copy_trading", lambda *a, **k: copy_calls.update(n=copy_calls["n"] + 1))
        bot._tick_copy_only()
        assert copy_calls["n"] == 0

    def test_opens_trade_via_copy_only(self, monkeypatch):
        """End-to-end: _tick_copy_only mirrors a new lead position."""
        bot = self._make_bot(monkeypatch)
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})
        monkeypatch.setattr(
            bot._client, "get_copy_trader_positions",
            lambda tid: [{"instId": "BTC-USDT", "side": "long", "pos": "0.1"}],
        )
        bot._tick_copy_only()
        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1
        assert open_trades[0]["direction"] == "LONG"


# ── _run_loop uses fast polling for copy trading ─────────────────────────────

class TestRunLoopCopyPolling:
    """Verify _run_loop dispatches to _tick_copy_only with short sleep."""

    def test_run_loop_calls_tick_copy_only_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.set_copy_trading_config(enabled=True, trader_id="leader1")

        copy_only_calls = {"n": 0}
        tick_calls = {"n": 0}

        def fake_copy_only():
            copy_only_calls["n"] += 1
            bot._running = False  # stop after first iteration

        def fake_tick():
            tick_calls["n"] += 1
            bot._running = False

        monkeypatch.setattr(bot, "_tick_copy_only", fake_copy_only)
        monkeypatch.setattr(bot, "_tick", fake_tick)
        # Make stop_event.wait return True immediately so loop doesn't block.
        monkeypatch.setattr(bot._stop_event, "wait", lambda timeout=None: True)

        bot._running = True
        bot._run_loop()

        assert copy_only_calls["n"] == 1
        assert tick_calls["n"] == 0

    def test_run_loop_calls_tick_when_copy_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.set_copy_trading_config(enabled=False, trader_id="")

        copy_only_calls = {"n": 0}
        tick_calls = {"n": 0}

        def fake_copy_only():
            copy_only_calls["n"] += 1
            bot._running = False

        def fake_tick():
            tick_calls["n"] += 1
            bot._running = False

        monkeypatch.setattr(bot, "_tick_copy_only", fake_copy_only)
        monkeypatch.setattr(bot, "_tick", fake_tick)
        monkeypatch.setattr(bot._stop_event, "wait", lambda timeout=None: True)

        bot._running = True
        bot._run_loop()

        assert tick_calls["n"] == 1
        assert copy_only_calls["n"] == 0

    def test_run_loop_uses_copy_sync_seconds_timeout(self, monkeypatch):
        """When copy trading, the stop_event.wait uses COPY_SYNC_SECONDS."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.set_copy_trading_config(enabled=True, trader_id="leader1")
        monkeypatch.setattr(bot, "_tick_copy_only", lambda: None)

        wait_timeouts = []
        orig_wait = bot._stop_event.wait

        def capture_wait(timeout=None):
            wait_timeouts.append(timeout)
            bot._running = False  # stop after first iteration
            return True

        monkeypatch.setattr(bot._stop_event, "wait", capture_wait)

        bot._running = True
        bot._run_loop()

        assert wait_timeouts[0] == 5
