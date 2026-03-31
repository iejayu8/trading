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
