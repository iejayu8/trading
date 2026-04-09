"""Robustness tests for TradingBot protections."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import database as db
from bot import TradingBot
from strategy import Signal


class TestBotRobustness:
    def setup_method(self):
        # Silence logs and side effects in tests.
        self._orig_log_event = db.log_event
        self._orig_update = db.update_bot_status
        self._orig_init = db.init_db
        db.log_event = lambda *args, **kwargs: None
        db.update_bot_status = lambda *args, **kwargs: None
        db.init_db = lambda *args, **kwargs: None

    def teardown_method(self):
        db.log_event = self._orig_log_event
        db.update_bot_status = self._orig_update
        db.init_db = self._orig_init

    def test_atomic_entry_no_db_open_when_exchange_order_fails(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        opened = []
        monkeypatch.setattr(db, "open_trade", lambda *a, **k: opened.append(1) or 1)
        monkeypatch.setattr(bot._client, "set_leverage", lambda *a, **k: {"code": "0"})

        def fail_place_order(*args, **kwargs):
            raise RuntimeError("exchange down")

        monkeypatch.setattr(bot._client, "place_order", fail_place_order)

        bot._enter_trade(Signal.LONG, price=100.0, equity=1000.0)

        assert opened == []

    def test_idempotent_client_order_id_is_sent(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        captured = {}

        monkeypatch.setattr(bot._client, "set_leverage", lambda *a, **k: {"code": "0"})

        def ok_place_order(*args, **kwargs):
            captured.update(kwargs)
            return {"code": "0", "msg": "success", "data": [{"orderId": "123"}]}

        monkeypatch.setattr(bot._client, "place_order", ok_place_order)
        monkeypatch.setattr(db, "open_trade", lambda *a, **k: 77)

        bot._enter_trade(Signal.SHORT, price=200.0, equity=1500.0)

        assert "client_order_id" in captured
        assert isinstance(captured["client_order_id"], str)
        assert captured["client_order_id"].startswith("bot-BTC-USDT-")

    def test_retry_wrapper_retries_then_succeeds(self):
        bot = TradingBot(symbol="BTC-USDT")
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return "ok"

        result = bot._call_with_retries(flaky, label="flaky_call")

        assert result == "ok"
        assert attempts["n"] == 3

    def test_exchange_position_detection(self, monkeypatch):
        bot = TradingBot(symbol="BTC-USDT")

        monkeypatch.setattr(bot._client, "get_positions", lambda symbol: [{"positions": "0"}])
        assert bot._has_exchange_open_position() is False

        monkeypatch.setattr(bot._client, "get_positions", lambda symbol: [{"positions": "0.01"}])
        assert bot._has_exchange_open_position() is True

    def test_reconciliation_closes_stale_local_open_trades(self, monkeypatch):
        bot = TradingBot(symbol="BTC-USDT")

        closed_calls = []
        monkeypatch.setattr(
            db,
            "close_trade",
            lambda trade_id, exit_price, pnl: closed_calls.append((trade_id, exit_price, pnl)),
        )

        local = [
            {"id": 10, "direction": "LONG", "entry_price": 90.0, "size": 1.0},
            {"id": 11, "direction": "SHORT", "entry_price": 110.0, "size": 2.0},
        ]
        out = bot._reconcile_local_open_trades(local, exchange_has_position=False, mark_price=100.0)

        assert out == []
        assert [call[0] for call in closed_calls] == [10, 11]
        assert [call[1] for call in closed_calls] == [100.0, 100.0]
        assert [call[2] for call in closed_calls] == [10.0, 20.0]

    def test_paper_mode_skips_exchange_for_entry(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        bot = TradingBot(symbol="BTC-USDT")

        called = {"set_leverage": 0, "place_order": 0}

        def should_not_call_set_leverage(*args, **kwargs):
            called["set_leverage"] += 1
            raise AssertionError("set_leverage should not be called in paper mode")

        def should_not_call_place_order(*args, **kwargs):
            called["place_order"] += 1
            raise AssertionError("place_order should not be called in paper mode")

        monkeypatch.setattr(bot._client, "set_leverage", should_not_call_set_leverage)
        monkeypatch.setattr(bot._client, "place_order", should_not_call_place_order)

        opened = []
        monkeypatch.setattr(db, "open_trade", lambda *a, **k: opened.append(1) or 42)

        bot._enter_trade(Signal.LONG, price=100.0, equity=1000.0)

        assert opened == [1]
        assert called["set_leverage"] == 0
        assert called["place_order"] == 0

    def test_real_mode_close_failure_keeps_trade_open(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        closed_calls = []
        monkeypatch.setattr(db, "close_trade", lambda *a, **k: closed_calls.append((a, k)))

        def fail_close(*args, **kwargs):
            raise RuntimeError("close failed")

        monkeypatch.setattr(bot._client, "place_order", fail_close)

        trade = {
            "id": 101,
            "direction": Signal.LONG,
            "entry_price": 100.0,
            "sl_price": 95.0,
            "tp_price": 105.0,
            "size": 1.0,
        }
        bot._manage_open_trades([trade], current_price=106.0)

        # Must stay open locally when exchange close fails.
        assert closed_calls == []

    def test_real_mode_close_rejected_keeps_trade_open(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        closed_calls = []
        monkeypatch.setattr(db, "close_trade", lambda *a, **k: closed_calls.append((a, k)))

        monkeypatch.setattr(
            bot._client,
            "place_order",
            lambda *args, **kwargs: {"code": "51001", "msg": "rejected"},
        )

        trade = {
            "id": 102,
            "direction": Signal.LONG,
            "entry_price": 100.0,
            "sl_price": 95.0,
            "tp_price": 105.0,
            "size": 1.0,
        }
        bot._manage_open_trades([trade], current_price=106.0)

        # Must stay open locally when exchange rejects close.
        assert closed_calls == []

    def test_real_mode_close_success_closes_trade_locally(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")
        bot = TradingBot(symbol="BTC-USDT")

        closed_calls = []
        monkeypatch.setattr(
            db,
            "close_trade",
            lambda trade_id, exit_price, pnl: closed_calls.append((trade_id, exit_price, pnl)),
        )
        monkeypatch.setattr(
            bot._client,
            "place_order",
            lambda *args, **kwargs: {"code": "0", "msg": "success"},
        )

        trade = {
            "id": 103,
            "direction": Signal.LONG,
            "entry_price": 100.0,
            "sl_price": 95.0,
            "tp_price": 105.0,
            "size": 1.0,
        }
        bot._manage_open_trades([trade], current_price=106.0)

        assert len(closed_calls) == 1
        assert closed_calls[0][0] == 103


# ── Equity update on trade close ───────────────────────────────────────────────

class TestEquityUpdateOnTradeClose:
    """Equity in bot_status must be refreshed immediately after a trade closes
    in both paper and real trading mode so the dashboard never shows a stale value."""

    def setup_method(self):
        import tempfile
        import database as _db
        self._tmp = tempfile.mkdtemp()
        _db.DB_PATH = Path(self._tmp) / "test_equity.db"
        _db.init_db()

    def teardown_method(self):
        import database as _db
        for p in Path(self._tmp).glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            Path(self._tmp).rmdir()
        except OSError:
            pass

    def test_paper_equity_updated_immediately_on_tp_hit(self, monkeypatch):
        """After a TP is hit in paper mode the bot_status equity must equal
        PAPER_START_EQUITY + realised PnL without waiting for the next tick."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        bot = TradingBot(symbol="BTC-USDT")

        trade_id = _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=110.0)

        status = _db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None, "equity must be set after trade close"
        # PnL for LONG: (exit - entry) * size = (110-100)*1 = 10
        assert abs(status["equity"] - 1010.0) < 0.01, (
            f"Expected equity ~1010.0, got {status['equity']}"
        )

    def test_paper_equity_updated_immediately_on_sl_hit(self, monkeypatch):
        """A losing trade (SL hit) must reduce equity immediately."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        bot = TradingBot(symbol="BTC-USDT")

        trade_id = _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=94.0)

        status = _db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None
        # PnL = (94-100)*1 = -6  →  equity = 994
        assert status["equity"] < 1000.0, (
            "equity must decrease after a losing trade"
        )

    def test_paper_equity_updated_on_reconciliation_close(self, monkeypatch):
        """Stale trades closed during reconciliation must also update equity."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "papertrading")
        monkeypatch.setattr(config, "PAPER_START_EQUITY", 1000.0)

        bot = TradingBot(symbol="BTC-USDT")

        _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        bot._reconcile_local_open_trades(
            [trade], exchange_has_position=False, mark_price=105.0
        )

        status = _db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None
        # PnL = (105-100)*1 = 5  →  equity = 1005
        assert status["equity"] > 1000.0

    def test_real_equity_updated_immediately_on_tp_hit(self, monkeypatch):
        """In real trading mode equity must be re-fetched from the exchange
        immediately after a TP closes so the dashboard reflects new balance."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        bot = TradingBot(symbol="BTC-USDT")

        # Simulate exchange returning updated balance after close.
        def mock_get_balance():
            return {"equity": "1025.50"}

        monkeypatch.setattr(bot._client, "get_balance", mock_get_balance)

        # Exchange close order succeeds.
        monkeypatch.setattr(
            bot._client,
            "place_order",
            lambda *a, **k: {"code": "0"},
        )

        _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        bot._manage_open_trades([trade], current_price=110.0)

        status = _db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None
        assert abs(float(status["equity"]) - 1025.50) < 0.01, (
            f"Expected equity ~1025.50 from exchange, got {status['equity']}"
        )

    def test_real_equity_updated_on_reconciliation_close(self, monkeypatch):
        """Reconciliation close in real mode must also refresh equity."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        bot = TradingBot(symbol="BTC-USDT")

        def mock_get_balance():
            return {"equity": "990.00"}

        monkeypatch.setattr(bot._client, "get_balance", mock_get_balance)

        _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        bot._reconcile_local_open_trades(
            [trade], exchange_has_position=False, mark_price=98.0
        )

        status = _db.get_bot_status("BTC-USDT")
        assert status["equity"] is not None
        assert abs(float(status["equity"]) - 990.00) < 0.01

    def test_real_equity_balance_failure_is_tolerated(self, monkeypatch):
        """If the exchange balance call fails after close, equity stays stale
        but no exception is raised – the next tick will correct it."""
        import config
        import database as _db

        monkeypatch.setattr(config, "TRADING_MODE", "realtrading")

        bot = TradingBot(symbol="BTC-USDT")

        def fail_get_balance():
            raise RuntimeError("exchange timeout")

        monkeypatch.setattr(bot._client, "get_balance", fail_get_balance)
        monkeypatch.setattr(
            bot._client,
            "place_order",
            lambda *a, **k: {"code": "0"},
        )

        _db.open_trade(
            symbol="BTC-USDT",
            direction="LONG",
            entry_price=100.0,
            size=1.0,
            sl_price=95.0,
            tp_price=110.0,
            leverage=5,
        )
        trade = _db.get_open_trades("BTC-USDT")[0]

        # Must not raise even though get_balance fails.
        bot._manage_open_trades([trade], current_price=110.0)

        # Trade should still be closed in DB.
        open_trades = _db.get_open_trades("BTC-USDT")
        assert open_trades == [], "trade must be locally closed even when balance fetch fails"

