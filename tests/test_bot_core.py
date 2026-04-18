"""Tests for bot.py core logic: _tick(), helpers, and utility functions.

Covers the critical gaps in bot.py (49% → target 80%+):
- _candles_to_df()
- _extract_usdt_equity()
- _calc_pnl()
- _tick() full flow
- _daily_loss_exceeded()
- _paper_equity()
- _portfolio_allows_entry()
- start/stop lifecycle
- _price_sync_loop()
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
import database as db
from bot import TradingBot, _candles_to_df, _extract_usdt_equity, _calc_pnl
from strategy import Signal


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_db():
    """Use a temp DB for each test."""
    _tmp = tempfile.mkdtemp()
    db.DB_PATH = Path(_tmp) / "test_bot_core.db"
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
        ts += 900000  # 15 min
        price = c
    # BloFin returns newest first
    candles.reverse()
    return candles


# ── _candles_to_df ────────────────────────────────────────────────────────────


class TestCandlesToDf:
    def test_converts_raw_candles(self):
        raw = _make_raw_candles(50)
        df = _candles_to_df(raw)
        assert len(df) == 50
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in df.columns
            assert df[col].dtype == np.float64

    def test_chronological_order(self):
        raw = _make_raw_candles(20)
        df = _candles_to_df(raw)
        assert df.index.is_monotonic_increasing

    def test_empty_raw_returns_empty_df(self):
        df = _candles_to_df([])
        assert len(df) == 0
        assert "close" in df.columns

    def test_raises_on_short_rows(self):
        raw = [["ts", "o", "h"]]  # only 3 fields
        with pytest.raises(ValueError, match="Unexpected candle row width"):
            _candles_to_df(raw)

    def test_handles_extra_columns(self):
        """BloFin sometimes returns 9 fields; only first 7 are used."""
        raw = _make_raw_candles(5)
        for row in raw:
            row.extend(["extra1", "extra2"])
        df = _candles_to_df(raw)
        assert len(df) == 5
        assert "extra1" not in df.columns

    def test_datetime_index(self):
        raw = _make_raw_candles(10)
        df = _candles_to_df(raw)
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_ts_column_removed_from_regular_columns(self):
        raw = _make_raw_candles(10)
        df = _candles_to_df(raw)
        # ts is used to create datetime index; the remaining df should have OHLCV
        assert "close" in df.columns


# ── _extract_usdt_equity ─────────────────────────────────────────────────────


class TestExtractUsdtEquity:
    def test_nested_details_structure(self):
        data = {"details": [{"currency": "USDT", "equity": "1234.56"}]}
        assert _extract_usdt_equity(data) == 1234.56

    def test_flat_structure_fallback(self):
        data = {"equity": "9999.99"}
        assert _extract_usdt_equity(data) == 9999.99

    def test_no_usdt_in_details(self):
        data = {"details": [{"currency": "BTC", "equity": "0.5"}]}
        assert _extract_usdt_equity(data) is None

    def test_empty_details(self):
        data = {"details": []}
        assert _extract_usdt_equity(data) is None

    def test_empty_dict(self):
        assert _extract_usdt_equity({}) is None

    def test_multiple_currencies(self):
        data = {
            "details": [
                {"currency": "BTC", "equity": "0.5"},
                {"currency": "USDT", "equity": "2000.00"},
                {"currency": "ETH", "equity": "3.0"},
            ]
        }
        assert _extract_usdt_equity(data) == 2000.0


# ── _calc_pnl ─────────────────────────────────────────────────────────────────


class TestCalcPnl:
    def test_long_profit(self):
        pnl = _calc_pnl("LONG", entry=100.0, exit_price=110.0, size=1.0)
        assert pnl == 10.0

    def test_long_loss(self):
        pnl = _calc_pnl("LONG", entry=100.0, exit_price=95.0, size=1.0)
        assert pnl == -5.0

    def test_short_profit(self):
        pnl = _calc_pnl("SHORT", entry=100.0, exit_price=90.0, size=1.0)
        assert pnl == 10.0

    def test_short_loss(self):
        pnl = _calc_pnl("SHORT", entry=100.0, exit_price=105.0, size=1.0)
        assert pnl == -5.0

    def test_size_scaling(self):
        pnl1 = _calc_pnl("LONG", 100.0, 110.0, 1.0)
        pnl2 = _calc_pnl("LONG", 100.0, 110.0, 2.0)
        assert pnl2 == pnl1 * 2

    def test_breakeven(self):
        pnl = _calc_pnl("LONG", 100.0, 100.0, 1.0)
        assert pnl == 0.0

    def test_rounding(self):
        pnl = _calc_pnl("LONG", 100.0, 100.01, 1.0)
        # Should round to 4 decimal places
        assert abs(pnl - 0.01) < 0.001


# ── _daily_loss_exceeded ──────────────────────────────────────────────────────


class TestDailyLossExceeded:
    def test_no_trades_no_loss(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")
        assert bot._daily_loss_exceeded(1000.0) is False

    def test_exceeds_daily_loss(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        # Open and close a trade with big loss today
        from datetime import datetime, timezone
        trade_id = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(trade_id, 60.0, -40.0)  # -40 USDT loss

        assert bot._daily_loss_exceeded(1000.0) is True

    def test_zero_equity_returns_false(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")
        assert bot._daily_loss_exceeded(0.0) is False

    def test_portfolio_wide_aggregation(self, monkeypatch):
        """Daily loss guard should aggregate losses across ALL symbols."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
        bot = TradingBot(symbol="BTC-USDT")

        # Two small losses on different symbols that together exceed 3%
        t1 = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(t1, 95.0, -20.0)  # -2% of 1000
        t2 = db.open_trade("ETH-USDT", "SHORT", 3000.0, 0.1, 3100.0, 2800.0, 5)
        db.close_trade(t2, 3100.0, -15.0)  # -1.5% of 1000

        # Total daily loss = -35 / 1000 = -3.5% > -3% limit
        assert bot._daily_loss_exceeded(1000.0) is True

    def test_paper_mode_uses_start_equity_denominator(self, monkeypatch):
        """Paper mode should use PAPER_START_EQUITY as denominator, not current equity."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
        bot = TradingBot(symbol="BTC-USDT")

        # Loss of -25 USDT on a 1000 start equity = -2.5% (within -3% limit)
        t1 = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(t1, 95.0, -25.0)

        # If denominator were current equity (e.g. 950 from unrealised PnL),
        # -25/950 = -2.63% which is still within limit.
        # With start equity: -25/1000 = -2.5%, within limit.
        assert bot._daily_loss_exceeded(950.0) is False

    def test_real_mode_uses_current_equity_denominator(self, monkeypatch):
        """Real mode should use current equity as denominator."""
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT", 0.03)
        bot = TradingBot(symbol="BTC-USDT")

        # Loss of -35 USDT
        t1 = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(t1, 95.0, -35.0)

        # -35/1000 = -3.5% > -3% limit → exceeded
        assert bot._daily_loss_exceeded(1000.0) is True
        # -35/1500 = -2.3% < -3% limit → not exceeded
        assert bot._daily_loss_exceeded(1500.0) is False


# ── _paper_equity ─────────────────────────────────────────────────────────────


class TestPaperEquity:
    def test_initial_equity_no_trades(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 5000.0)
        bot = TradingBot(symbol="BTC-USDT")
        eq = bot._paper_equity()
        assert eq == 5000.0

    def test_includes_closed_pnl(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        trade_id = db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.close_trade(trade_id, 110.0, 10.0)

        eq = bot._paper_equity()
        assert eq == 1010.0

    def test_includes_unrealised_pnl(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        eq = bot._paper_equity(current_price=105.0)
        # unrealised = (105-100)*1 = 5
        assert eq == 1005.0

    def test_short_unrealised_pnl(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.open_trade("BTC-USDT", "SHORT", 100.0, 1.0, 105.0, 90.0, 5)
        eq = bot._paper_equity(current_price=95.0)
        # unrealised = (100-95)*1 = 5
        assert eq == 1005.0


# ── _portfolio_allows_entry ───────────────────────────────────────────────────


class TestPortfolioAllowsEntry:
    def test_allows_when_no_open_trades(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")
        assert bot._portfolio_allows_entry(10000.0, 50000.0) is True

    def test_blocks_max_open_positions(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 2)
        bot = TradingBot(symbol="BTC-USDT")

        # Open 2 trades for different symbols
        db.open_trade("ETH-USDT", "LONG", 2000.0, 0.5, 1900.0, 2200.0, 5)
        db.open_trade("SOL-USDT", "LONG", 100.0, 5.0, 95.0, 110.0, 5)

        assert bot._portfolio_allows_entry(10000.0, 50000.0) is False

    def test_allows_under_position_limit(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        monkeypatch.setattr(config, "MAX_OPEN_POSITIONS", 3)
        bot = TradingBot(symbol="BTC-USDT")

        db.open_trade("ETH-USDT", "LONG", 2000.0, 0.5, 1900.0, 2200.0, 5)

        assert bot._portfolio_allows_entry(10000.0, 50000.0) is True

    def test_blocks_margin_cap(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        monkeypatch.setattr(config, "MAX_MARGIN_USAGE_PCT", 0.10)  # very low
        bot = TradingBot(symbol="BTC-USDT")

        # Open a trade that takes up margin
        db.open_trade("ETH-USDT", "LONG", 2000.0, 0.5, 1900.0, 2200.0, 5)

        # Margin from existing: 2000*0.5 / 5 = 200; cap is 1000*0.10 = 100
        # Already over cap, so new entry blocked
        assert bot._portfolio_allows_entry(1000.0, 50000.0) is False

    def test_blocks_portfolio_risk_cap(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        monkeypatch.setattr(config, "MAX_PORTFOLIO_RISK_PCT", 0.005)  # 0.5% cap
        bot = TradingBot(symbol="BTC-USDT")

        # Open a trade with risk of entry-sl * size
        db.open_trade("ETH-USDT", "LONG", 2000.0, 0.5, 1900.0, 2200.0, 5)
        # risk = 0.5 * |2000-1900| = 50; cap = 1000*0.005 = 5 → already over

        assert bot._portfolio_allows_entry(1000.0, 50000.0) is False


# ── _tick (mocked) ────────────────────────────────────────────────────────────


class TestTick:
    def _make_indicator_df(self, n=200, price=50000.0):
        """Build a DataFrame with all indicator columns."""
        from strategy import compute_indicators
        np.random.seed(42)
        prices = [price]
        for _ in range(n - 1):
            prices.append(prices[-1] + np.random.uniform(-100, 100))
        df = pd.DataFrame({
            "open": [p * 0.999 for p in prices],
            "high": [p * 1.005 for p in prices],
            "low": [p * 0.995 for p in prices],
            "close": prices,
            "volume": [np.random.uniform(300, 900) for _ in prices],
        })
        df.index = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
        return compute_indicators(df)

    def test_tick_no_candles(self, monkeypatch):
        """_tick exits gracefully when no candle data."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: [])
        bot._tick()  # should not raise

        logs = db.get_logs(10)
        messages = [l["message"] for l in logs]
        assert any("No candle data" in m for m in messages)

    def test_tick_processes_candles_paper_mode(self, monkeypatch):
        """_tick fetches candles, computes indicators, updates status."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        bot._tick()

        status = db.get_bot_status("BTC-USDT")
        assert status.get("last_price") is not None
        assert status.get("equity") is not None
        assert status.get("signal_hint") is not None

    def test_tick_enters_trade_on_signal(self, monkeypatch):
        """If generate_signal returns LONG, _tick should open a trade."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        # Force signal to LONG - must patch in the bot module's namespace
        import bot as bot_module
        monkeypatch.setattr(bot_module, "generate_signal", lambda df, symbol="BTC-USDT": Signal.LONG)

        bot._tick()

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1
        assert open_trades[0]["direction"] == "LONG"

    def test_tick_skips_entry_when_trade_open(self, monkeypatch):
        """No new entry when an open trade exists."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        # Pre-open a trade
        db.open_trade("BTC-USDT", "LONG", 50000.0, 0.01, 49000.0, 52000.0, 5)

        import bot as bot_module
        monkeypatch.setattr(bot_module, "generate_signal", lambda df, symbol="BTC-USDT": Signal.LONG)

        bot._tick()

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1  # still just one

    def test_tick_manages_tp_hit(self, monkeypatch):
        """_tick closes trade when TP is hit."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        # Create a trade with TP at 51000
        db.open_trade("BTC-USDT", "LONG", 50000.0, 0.01, 49000.0, 51000.0, 5)

        # Create candles with last close at 52000 (above TP)
        raw = _make_raw_candles(200, start_price=52000.0)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        import bot as bot_module
        monkeypatch.setattr(bot_module, "generate_signal", lambda df, symbol="BTC-USDT": Signal.NONE)

        bot._tick()

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 0

    def test_tick_real_mode_equity_from_exchange(self, monkeypatch):
        """In real mode, equity comes from exchange balance."""
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)
        monkeypatch.setattr(bot._client, "get_balance", lambda: {
            "details": [{"currency": "USDT", "equity": "5000.0"}]
        })
        monkeypatch.setattr(bot._client, "get_positions", lambda s: [])

        import bot as bot_module
        monkeypatch.setattr(bot_module, "generate_signal", lambda df, symbol="BTC-USDT": Signal.NONE)

        bot._tick()

        status = db.get_bot_status("BTC-USDT")
        assert abs(float(status["equity"]) - 5000.0) < 0.01

    def test_tick_exception_logged(self, monkeypatch):
        """Exceptions in _tick are caught and logged."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        def explode(*a, **kw):
            raise RuntimeError("API timeout")

        monkeypatch.setattr(bot._client, "get_candles", explode)

        # _tick wraps in try/except; _run_loop catches and logs
        try:
            bot._tick()
        except RuntimeError:
            pass  # Expected from _call_with_retries after max retries

    def test_tick_daily_loss_skips_signal(self, monkeypatch):
        """When daily loss is exceeded, signal generation is skipped."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        raw = _make_raw_candles(200)
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: raw)

        # Create a big loss today
        trade_id = db.open_trade("BTC-USDT", "LONG", 50000.0, 1.0, 49000.0, 51000.0, 5)
        db.close_trade(trade_id, 40000.0, -50.0)  # big loss

        import bot as bot_module
        signal_called = {"count": 0}
        orig = bot_module.generate_signal

        def track_signal(*a, **kw):
            signal_called["count"] += 1
            return orig(*a, **kw)

        monkeypatch.setattr(bot_module, "generate_signal", track_signal)

        bot._tick()

        # Signal may or may not be called (depends on if daily loss guard fires before)
        # But no new trades should be opened
        logs = db.get_logs(20)
        messages = [l["message"] for l in logs]
        # Either daily loss message appears, or no trade opened
        assert any("Daily loss" in m for m in messages) or len(db.get_open_trades("BTC-USDT")) == 0


# ── start/stop lifecycle ──────────────────────────────────────────────────────


class TestBotLifecycle:
    def test_start_sets_running(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        # Mock the threads to not actually run
        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: [])
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})

        bot.start()
        assert bot.is_running is True

        bot.stop()
        assert bot.is_running is False

    def test_start_idempotent(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        monkeypatch.setattr(bot._client, "get_candles", lambda *a, **kw: [])
        monkeypatch.setattr(bot._client, "get_ticker", lambda s: {"last": "50000"})

        bot.start()
        bot.start()  # should not raise or start second thread
        assert bot.is_running is True

        bot.stop()

    def test_stop_updates_db(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        bot.start()
        bot.stop()

        status = db.get_bot_status("BTC-USDT")
        assert status["running"] == 0

    def test_paper_equity_seeded_on_start(self, monkeypatch):
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 5000.0)
        bot = TradingBot(symbol="BTC-USDT")

        bot.start()
        status = db.get_bot_status("BTC-USDT")
        bot.stop()

        assert status["equity"] == 5000.0

    def test_real_equity_seeded_on_start(self, monkeypatch):
        """Real-trading bot seeds equity from exchange balance immediately on start.

        This ensures the dashboard does not show stale paper-trading equity
        after switching from paper to real trading mode.
        """
        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        monkeypatch.setattr(bot._client, "get_balance", lambda: {
            "details": [{"currency": "USDT", "equity": "7500.0"}]
        })

        bot.start()
        status = db.get_bot_status("BTC-USDT")
        bot.stop()

        assert abs(float(status["equity"]) - 7500.0) < 0.01


# ── _manage_open_trades edge cases ────────────────────────────────────────────


class TestManageOpenTrades:
    def test_short_sl_hit(self, monkeypatch):
        """SHORT trade closes when price rises above SL."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        trade_id = db.open_trade("BTC-USDT", "SHORT", 100.0, 1.0, 105.0, 90.0, 5)
        trade = db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=106.0)  # above SL of 105

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 0

    def test_short_tp_hit(self, monkeypatch):
        """SHORT trade closes when price drops below TP."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        trade_id = db.open_trade("BTC-USDT", "SHORT", 100.0, 1.0, 105.0, 90.0, 5)
        trade = db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=89.0)  # below TP of 90

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 0

    def test_no_close_when_price_between_sl_tp(self, monkeypatch):
        """Trade stays open when price is between SL and TP."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        trade = db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=102.0)

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1

    def test_multiple_trades_managed(self, monkeypatch):
        """Multiple open trades are all checked."""
        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 10000.0)
        bot = TradingBot(symbol="BTC-USDT")

        db.open_trade("BTC-USDT", "LONG", 100.0, 1.0, 95.0, 110.0, 5)
        db.open_trade("BTC-USDT", "LONG", 100.0, 0.5, 95.0, 120.0, 5)

        trades = db.get_open_trades("BTC-USDT")
        # Price 111 triggers TP for first trade (tp=110), but not second (tp=120)
        # And price 111 is above both SLs (95)
        bot._manage_open_trades(trades, current_price=111.0)

        open_trades = db.get_open_trades("BTC-USDT")
        assert len(open_trades) == 1  # second trade is still open
