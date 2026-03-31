"""
Tests for database module.
"""

import sys
import tempfile
from pathlib import Path

import pytest

# Redirect DB to a temp file during tests
import database

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


class TestDatabase:
    def setup_method(self):
        """Use a unique temp DB per test to avoid Windows file lock races."""
        self._tmp_dir = tempfile.mkdtemp()
        database.DB_PATH = Path(self._tmp_dir) / "test_trading_bot.db"
        database.init_db()

    def teardown_method(self):
        # Best-effort cleanup for temp directory created in setup_method.
        for p in Path(self._tmp_dir).glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            Path(self._tmp_dir).rmdir()
        except OSError:
            pass

    def test_init_creates_tables(self):
        with database._connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"trades", "bot_logs", "bot_status"}.issubset(tables)

    def test_open_and_close_trade(self):
        trade_id = database.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=40000.0,
            size=0.001,
            sl_price=39400.0,
            tp_price=41200.0,
            leverage=5,
        )
        assert trade_id is not None

        open_trades = database.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1

        database.close_trade(trade_id, exit_price=41200.0, pnl=6.0)

        open_trades = database.get_open_trades("BTC-USDT")
        assert len(open_trades) == 0

        history = database.get_trade_history("BTC-USDT", limit=10)
        assert len(history) == 1
        assert history[0]["status"] == "CLOSED"

    def test_trade_stats_win_rate(self):
        # Two wins, one loss
        for pnl, exit_price in [(5.0, 41200.0), (3.0, 41000.0), (-2.0, 39400.0)]:
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001,
                                      39400.0, 41200.0, 5)
            database.close_trade(tid, exit_price, pnl)

        stats = database.get_trade_stats()
        assert stats["total"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert abs(stats["win_rate"] - 66.67) < 0.1

    def test_trade_stats_per_symbol(self):
        """Per-symbol stats only count trades for that symbol."""
        tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39400.0, 41200.0, 5)
        database.close_trade(tid, 41200.0, 5.0)
        tid = database.open_trade("ETH-USDT", "SHORT", 2000.0, 0.01, 2050.0, 1950.0, 5)
        database.close_trade(tid, 1950.0, 2.0)

        btc_stats = database.get_trade_stats(symbol="BTC-USDT")
        eth_stats = database.get_trade_stats(symbol="ETH-USDT")
        all_stats = database.get_trade_stats()

        assert btc_stats["total"] == 1
        assert eth_stats["total"] == 1
        assert all_stats["total"] == 2

    def test_log_event_and_get_logs(self):
        database.log_event("Test message", level="INFO")
        database.log_event("Warning message", level="WARNING")

        logs = database.get_logs(limit=10)
        assert len(logs) == 2
        assert logs[0]["level"] == "WARNING"  # newest first

    def test_bot_status_update(self):
        database.update_bot_status(symbol="BTC-USDT", running=1, last_signal="LONG", last_price=42000.0)
        status = database.get_bot_status(symbol="BTC-USDT")
        assert status["running"] == 1
        assert status["last_signal"] == "LONG"
        assert status["last_price"] == 42000.0

    def test_bot_status_per_symbol_isolation(self):
        """BTC and ETH status rows are independent."""
        database.update_bot_status(symbol="BTC-USDT", running=1, last_signal="LONG")
        database.update_bot_status(symbol="ETH-USDT", running=0, last_signal="SHORT")

        btc = database.get_bot_status(symbol="BTC-USDT")
        eth = database.get_bot_status(symbol="ETH-USDT")

        assert btc["running"] == 1
        assert btc["last_signal"] == "LONG"
        assert eth["running"] == 0
        assert eth["last_signal"] == "SHORT"
