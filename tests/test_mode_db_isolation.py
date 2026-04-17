"""
test_mode_db_isolation.py – Tests for per-mode database isolation.

Verifies that the four operating modes (papertrading/realtrading ×
custom-strategy/copy-trading) each use a separate SQLite file, and that
switching modes presents only the data belonging to the active mode.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
import database as db


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolate_db_and_config(tmp_path):
    """Use a temp dir for all DB files and save/restore config state."""
    old_dir, old_stem, old_path = db._DB_DIR, db._DB_STEM, db.DB_PATH
    old_mode = config.TRADING_MODE
    old_copy = config.COPY_TRADING_ENABLED

    db._DB_DIR = tmp_path
    db._DB_STEM = "test_iso"
    db.DB_PATH = tmp_path / "test_iso.db"
    db.init_db()

    yield tmp_path

    db._DB_DIR, db._DB_STEM, db.DB_PATH = old_dir, old_stem, old_path
    config.TRADING_MODE = old_mode
    config.COPY_TRADING_ENABLED = old_copy


# ── _mode_db_path() unit tests ───────────────────────────────────────────────

class TestModeDbPath:
    def test_paper_custom(self, tmp_path):
        p = db._mode_db_path("papertrading", False)
        assert p == tmp_path / "test_iso_paper_custom.db"

    def test_paper_copy(self, tmp_path):
        p = db._mode_db_path("papertrading", True)
        assert p == tmp_path / "test_iso_paper_copy.db"

    def test_real_custom(self, tmp_path):
        p = db._mode_db_path("realtrading", False)
        assert p == tmp_path / "test_iso_real_custom.db"

    def test_real_copy(self, tmp_path):
        p = db._mode_db_path("realtrading", True)
        assert p == tmp_path / "test_iso_real_copy.db"

    def test_unknown_mode_treated_as_real(self, tmp_path):
        """Any non-'papertrading' value is treated as 'real'."""
        p = db._mode_db_path("something_else", False)
        assert "real_custom" in p.name


# ── switch_db() unit tests ───────────────────────────────────────────────────

class TestSwitchDb:
    def test_switch_db_changes_path(self, tmp_path):
        db.switch_db("papertrading", False)
        assert db.DB_PATH == tmp_path / "test_iso_paper_custom.db"

        db.switch_db("realtrading", True)
        assert db.DB_PATH == tmp_path / "test_iso_real_copy.db"

    def test_switch_db_creates_tables(self, tmp_path):
        db.switch_db("papertrading", True)
        with db._db() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"trades", "bot_logs", "bot_status", "copy_trading_config"}.issubset(tables)

    def test_switch_db_resets_running_flags(self, tmp_path):
        db.switch_db("papertrading", False)
        db.update_bot_status("BTC-USDT", running=1)
        assert db.get_bot_status("BTC-USDT")["running"] == 1

        # Re-switch to same DB should reset flags
        db.switch_db("papertrading", False)
        assert db.get_bot_status("BTC-USDT")["running"] == 0

    def test_switch_db_idempotent(self, tmp_path):
        """Calling switch_db twice with the same args doesn't break anything."""
        db.switch_db("realtrading", False)
        db.log_event("first")
        db.switch_db("realtrading", False)
        # Log should survive (tables recreated with IF NOT EXISTS)
        logs = db.get_logs()
        assert len(logs) >= 1

    def test_each_mode_gets_separate_file(self, tmp_path):
        """All four modes produce distinct DB files."""
        paths = set()
        for mode in ("papertrading", "realtrading"):
            for copy in (False, True):
                db.switch_db(mode, copy)
                paths.add(db.DB_PATH)
        assert len(paths) == 4


# ── Data isolation between modes ─────────────────────────────────────────────

class TestDataIsolation:
    """Verify that trades, logs and status in one mode don't leak into another."""

    def test_trades_isolated_between_modes(self, tmp_path):
        # Paper + custom: create a trade
        db.switch_db("papertrading", False)
        db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)

        # Real + custom: should have no trades
        db.switch_db("realtrading", False)
        assert db.get_trade_history() == []
        assert db.get_open_trades() == []

        # Switch back to paper + custom: trade is still there
        db.switch_db("papertrading", False)
        assert len(db.get_open_trades()) == 1

    def test_logs_isolated_between_modes(self, tmp_path):
        db.switch_db("papertrading", False)
        db.log_event("paper log")

        db.switch_db("realtrading", False)
        assert db.get_logs() == []

        db.switch_db("papertrading", False)
        logs = db.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "paper log"

    def test_bot_status_isolated_between_modes(self, tmp_path):
        db.switch_db("papertrading", False)
        db.update_bot_status("BTC-USDT", last_signal="LONG", equity=5000.0)

        db.switch_db("realtrading", False)
        status = db.get_bot_status("BTC-USDT")
        # Fresh DB: default values
        assert status["last_signal"] == "NONE"
        assert status["equity"] is None

        db.switch_db("papertrading", False)
        status = db.get_bot_status("BTC-USDT")
        assert status["last_signal"] == "LONG"
        assert status["equity"] == 5000.0

    def test_copy_trading_config_isolated(self, tmp_path):
        db.switch_db("papertrading", True)
        db.set_copy_trading_config(enabled=True, trader_id="leader1")

        db.switch_db("papertrading", False)
        cfg = db.get_copy_trading_config()
        assert cfg["enabled"] is False  # default in a fresh DB

        db.switch_db("papertrading", True)
        cfg = db.get_copy_trading_config()
        assert cfg["enabled"] is True
        assert cfg["trader_id"] == "leader1"

    def test_stats_isolated_between_modes(self, tmp_path):
        # Paper mode: create 2 winning trades
        db.switch_db("papertrading", False)
        for _ in range(2):
            tid = db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
            db.close_trade(tid, 41600.0, 50.0)

        paper_stats = db.get_trade_stats()
        assert paper_stats["total"] == 2

        # Real mode: no trades
        db.switch_db("realtrading", False)
        real_stats = db.get_trade_stats()
        assert real_stats["total"] == 0

    def test_all_four_modes_independent(self, tmp_path):
        """Write a unique trade in each mode, verify each has exactly one."""
        modes = [
            ("papertrading", False),
            ("papertrading", True),
            ("realtrading", False),
            ("realtrading", True),
        ]
        prices = [10000.0, 20000.0, 30000.0, 40000.0]

        for (trading_mode, copy_trading), price in zip(modes, prices):
            db.switch_db(trading_mode, copy_trading)
            db.open_trade("BTC-USDT", "LONG", price, 0.001, price - 500, price + 500, 5)

        for (trading_mode, copy_trading), price in zip(modes, prices):
            db.switch_db(trading_mode, copy_trading)
            trades = db.get_open_trades()
            assert len(trades) == 1, f"Expected 1 trade in {trading_mode}/{copy_trading}"
            assert trades[0]["entry_price"] == price

    def test_reset_only_affects_current_mode(self, tmp_path):
        """reset_database() should only wipe the active mode's data."""
        db.switch_db("papertrading", False)
        db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)

        db.switch_db("realtrading", False)
        db.open_trade("BTC-USDT", "SHORT", 40000.0, 0.001, 41000.0, 38000.0, 5)
        db.reset_database()

        # Real mode should be empty
        assert db.get_open_trades() == []

        # Paper mode should still have its trade
        db.switch_db("papertrading", False)
        assert len(db.get_open_trades()) == 1


# ── API-level integration tests ──────────────────────────────────────────────

@pytest.fixture()
def app_client(tmp_path):
    """Flask test client with mode-aware temp DB paths."""
    db._DB_DIR = tmp_path
    db._DB_STEM = "test_isolation"
    db.DB_PATH = tmp_path / "test_isolation.db"
    db.init_db()

    import app as flask_app

    flask_app._bots.clear()
    flask_app.app.config["TESTING"] = True

    old_copy = config.COPY_TRADING_ENABLED

    with flask_app.app.test_client() as client:
        yield client

    flask_app._bots.clear()
    config.COPY_TRADING_ENABLED = old_copy


class TestApiModeIsolation:
    """End-to-end tests: switching modes via the API serves the correct data."""

    def test_trading_mode_switch_isolates_trades(self, app_client):
        import app as flask_app

        # Start in papertrading
        config.TRADING_MODE = "papertrading"
        db.switch_db("papertrading", config.COPY_TRADING_ENABLED)
        flask_app._bots.clear()

        # Create a trade in paper mode
        db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)

        # Verify trade is visible via API
        r = app_client.get("/api/trades").get_json()
        assert len(r) == 1

        # Switch to realtrading via API
        resp = app_client.post("/api/trading/mode", json={"mode": "realtrading"})
        assert resp.get_json()["ok"] is True

        # Trades should be empty in real mode
        r = app_client.get("/api/trades").get_json()
        assert len(r) == 0

        # Switch back to paper trading
        flask_app._bots.clear()
        resp = app_client.post("/api/trading/mode", json={"mode": "papertrading"})
        assert resp.get_json()["ok"] is True

        # Paper trade should be visible again
        r = app_client.get("/api/trades").get_json()
        assert len(r) == 1

    def test_copy_trading_toggle_isolates_trades(self, app_client):
        import app as flask_app

        # Start in papertrading + custom strategy
        config.TRADING_MODE = "papertrading"
        config.COPY_TRADING_ENABLED = False
        db.switch_db("papertrading", False)
        flask_app._bots.clear()

        # Create a trade in custom mode
        db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)

        r = app_client.get("/api/trades").get_json()
        assert len(r) == 1

        # Enable copy trading (switches to paper+copy DB)
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "leader1"},
        )
        assert resp.get_json()["ok"] is True

        # Trades should be empty in copy mode
        r = app_client.get("/api/trades").get_json()
        assert len(r) == 0

        # Disable copy trading (back to paper+custom DB)
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": False, "trader_id": ""},
        )
        assert resp.get_json()["ok"] is True

        # Custom trade should be visible again
        r = app_client.get("/api/trades").get_json()
        assert len(r) == 1

    def test_copy_trading_toggle_blocked_while_running(self, app_client):
        """Toggling copy trading while bots run should return 409."""
        import app as flask_app

        mock_bot = MagicMock()
        mock_bot.is_running = True
        flask_app._bots["BTC-USDT"] = mock_bot

        config.COPY_TRADING_ENABLED = False
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "leader1"},
        )
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["ok"] is False
        assert "Stop all bots" in data["message"]

    def test_copy_trading_update_without_toggle_works_while_running(self, app_client):
        """Updating trader_id without changing enabled state should work even
        when bots are running (no DB switch needed)."""
        import app as flask_app

        config.COPY_TRADING_ENABLED = True
        db.switch_db(config.TRADING_MODE, True)

        mock_bot = MagicMock()
        mock_bot.is_running = True
        flask_app._bots["BTC-USDT"] = mock_bot

        # Already enabled, just updating trader_id
        resp = app_client.post(
            "/api/copytrading/config",
            json={"enabled": True, "trader_id": "new_leader"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["trader_id"] == "new_leader"

    def test_stats_isolated_across_mode_switch(self, app_client):
        import app as flask_app

        # Paper mode: create + close a winning trade
        config.TRADING_MODE = "papertrading"
        db.switch_db("papertrading", config.COPY_TRADING_ENABLED)
        flask_app._bots.clear()

        tid = db.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        db.close_trade(tid, 41600.0, 50.0)

        stats = app_client.get("/api/stats").get_json()
        assert stats["total"] == 1
        assert stats["total_pnl"] == 50.0

        # Switch to real mode
        resp = app_client.post("/api/trading/mode", json={"mode": "realtrading"})
        assert resp.get_json()["ok"] is True

        stats = app_client.get("/api/stats").get_json()
        assert stats["total"] == 0
        assert stats["total_pnl"] == 0
