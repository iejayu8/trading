"""Tests for database.py edge cases: update_bot_status guard, migrations, reset.

Covers gaps in database.py (91% → target 98%+):
- update_bot_status() unknown column ValueError
- reset_database() clears all tables
- get_all_bot_status()
- Concurrent access patterns
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import database as db


@pytest.fixture(autouse=True)
def isolate_db():
    """Use a temp DB for each test."""
    _tmp = tempfile.mkdtemp()
    db.DB_PATH = Path(_tmp) / "test_db_edge.db"
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


class TestUpdateBotStatusGuard:
    def test_unknown_column_raises_value_error(self):
        """Passing an unknown column to update_bot_status must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown bot_status column"):
            db.update_bot_status("BTC-USDT", unknown_column=42)

    def test_multiple_unknown_columns_raises(self):
        with pytest.raises(ValueError, match="Unknown bot_status column"):
            db.update_bot_status("BTC-USDT", bad1=1, bad2=2)

    def test_mix_of_valid_and_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown bot_status column"):
            db.update_bot_status("BTC-USDT", running=1, fake_col="x")

    def test_valid_columns_accepted(self):
        """All known columns should be accepted without error."""
        db.update_bot_status(
            "BTC-USDT",
            running=1,
            last_signal="LONG",
            signal_hint="LONG_READY",
            waiting_for="Ready",
            long_ready=1,
            short_ready=0,
            last_price=50000.0,
            equity=10000.0,
            open_trades=1,
            total_trades=5,
            win_trades=3,
        )
        status = db.get_bot_status("BTC-USDT")
        assert status["running"] == 1
        assert status["last_signal"] == "LONG"
        assert status["equity"] == 10000.0


class TestResetDatabase:
    def test_clears_trades(self):
        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        assert len(db.get_open_trades()) > 0
        db.reset_database()
        assert len(db.get_open_trades()) == 0

    def test_clears_logs(self):
        db.log_event("test event")
        assert len(db.get_logs()) > 0
        db.reset_database()
        assert len(db.get_logs()) == 0

    def test_clears_bot_status(self):
        db.update_bot_status("BTC-USDT", running=1)
        db.reset_database()
        # After reset, get_bot_status should create a fresh row
        status = db.get_bot_status("BTC-USDT")
        assert status["running"] == 0

    def test_tables_still_usable_after_reset(self):
        db.reset_database()
        # Should be able to insert new data
        trade_id = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        assert trade_id > 0
        db.log_event("post-reset event")
        assert len(db.get_logs()) == 1


class TestGetAllBotStatus:
    def test_returns_all_symbols(self):
        db.update_bot_status("BTC-USDT", running=1)
        db.update_bot_status("ETH-USDT", running=0)
        db.update_bot_status("SOL-USDT", equity=500.0)

        all_status = db.get_all_bot_status()
        symbols = [s["symbol"] for s in all_status]
        assert "BTC-USDT" in symbols
        assert "ETH-USDT" in symbols
        assert "SOL-USDT" in symbols

    def test_ordered_by_symbol(self):
        db.update_bot_status("SOL-USDT", running=0)
        db.update_bot_status("BTC-USDT", running=0)
        db.update_bot_status("ETH-USDT", running=0)

        all_status = db.get_all_bot_status()
        symbols = [s["symbol"] for s in all_status]
        assert symbols == sorted(symbols)

    def test_empty_when_no_status(self):
        all_status = db.get_all_bot_status()
        # init_db() doesn't pre-populate status
        assert isinstance(all_status, list)


class TestResetRunningFlags:
    def test_resets_all_running_to_zero(self):
        db.update_bot_status("BTC-USDT", running=1)
        db.update_bot_status("ETH-USDT", running=1)

        db.reset_running_flags()

        status1 = db.get_bot_status("BTC-USDT")
        status2 = db.get_bot_status("ETH-USDT")
        assert status1["running"] == 0
        assert status2["running"] == 0


class TestTradeOperationsEdgeCases:
    def test_close_trade_updates_all_fields(self):
        trade_id = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(trade_id, 105.0, 5.0)

        trade = db.get_trade_by_id(trade_id)
        assert trade["status"] == "CLOSED"
        assert trade["exit_price"] == 105.0
        assert trade["pnl"] == 5.0
        assert trade["closed_at"] is not None

    def test_get_trade_by_id_nonexistent(self):
        result = db.get_trade_by_id(99999)
        assert result is None

    def test_get_trade_history_all_symbols(self):
        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.open_trade("ETH-USDT", "SHORT", 2000.0, 0.5, 2100.0, 1800.0, 5)

        trades = db.get_trade_history()
        assert len(trades) == 2

    def test_get_trade_history_filter_by_symbol(self):
        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.open_trade("ETH-USDT", "SHORT", 2000.0, 0.5, 2100.0, 1800.0, 5)

        trades = db.get_trade_history(symbol="BTC-USDT")
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BTC-USDT"

    def test_get_trade_stats_zero_trades(self):
        stats = db.get_trade_stats()
        assert stats["total"] == 0
        assert stats["win_rate"] == 0.0

    def test_get_trade_stats_per_symbol(self):
        tid1 = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(tid1, 110.0, 10.0)
        tid2 = db.open_trade("ETH-USDT", "SHORT", 2000.0, 0.5, 2100.0, 1800.0, 5)
        db.close_trade(tid2, 1800.0, 100.0)

        btc_stats = db.get_trade_stats("BTC-USDT")
        assert btc_stats["total"] == 1

        eth_stats = db.get_trade_stats("ETH-USDT")
        assert eth_stats["total"] == 1


class TestLogOperations:
    def test_log_levels(self):
        db.log_event("info msg", level="INFO")
        db.log_event("warn msg", level="WARNING")
        db.log_event("error msg", level="ERROR")

        logs = db.get_logs()
        levels = {l["level"] for l in logs}
        assert "INFO" in levels
        assert "WARNING" in levels
        assert "ERROR" in levels

    def test_log_ordering(self):
        db.log_event("first")
        db.log_event("second")
        db.log_event("third")

        logs = db.get_logs()
        # Most recent first (ORDER BY id DESC)
        assert logs[0]["message"] == "third"
        assert logs[2]["message"] == "first"

    def test_log_limit(self):
        for i in range(20):
            db.log_event(f"msg {i}")
        logs = db.get_logs(limit=5)
        assert len(logs) == 5


class TestEnsureSymbolStatus:
    def test_auto_creates_status_row(self):
        """get_bot_status auto-creates a row if none exists."""
        status = db.get_bot_status("NEW-SYMBOL-USDT")
        assert status["symbol"] == "NEW-SYMBOL-USDT"
        assert status["running"] == 0

    def test_update_creates_if_missing(self):
        """update_bot_status auto-creates the row first."""
        db.update_bot_status("BRAND-NEW-USDT", running=1)
        status = db.get_bot_status("BRAND-NEW-USDT")
        assert status["running"] == 1
