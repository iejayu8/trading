"""
test_api_frontend.py – Tests for all Flask API endpoints consumed by the frontend.

Covers every operation the frontend app.js performs against the backend:
  Functional tests (verifying correct behavior):
    - GET /api/symbols
    - GET /api/status (all symbols)
    - GET /api/status?symbol=X (single symbol)
    - GET /api/trades (all, per-symbol, with limit)
    - GET /api/trades/open (all, per-symbol)
    - GET /api/stats (all, per-symbol)
    - GET /api/config (default, per-symbol)
    - GET /api/logs (with limit)
    - POST /api/logs/clear
    - POST /api/bot/start (all bots, single symbol)
    - POST /api/bot/stop (all bots, single symbol)
    - GET /api/market/context (mocked exchange)
    - GET / (serve index.html)
    - GET /style.css (static file serving)

  Robustness tests (stability / edge-case hardening):
    - Invalid symbol in start/stop
    - Invalid limit parameter in /api/trades (should not 500)
    - Invalid limit in /api/market/context returns 400
    - market/context limit clamping (< 60 or > 200)
    - market/context with exchange failure returns 502
    - market/context with short candle payload returns 502
    - Start already-running bot returns 400
    - Stop non-running bot returns 400
    - Stop all when none running returns 400
    - Start all when all running returns 400
    - Static file with disallowed extension returns 404
    - Stats on empty DB returns valid structure
    - get_trade_stats win_rate denominator safety
    - Concurrent status requests (thread safety)
    - Trade history ordering and field completeness
    - Log levels preserved through API
"""

from __future__ import annotations

import sys
import threading
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import database


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture()
def app_client(tmp_path):
    """Flask test client with an isolated temp DB and clean bot registry."""
    database.DB_PATH = tmp_path / "test_frontend.db"
    database.init_db()

    import app as flask_app

    # Always start each test with an empty bot registry.
    flask_app._bots.clear()
    flask_app.app.config["TESTING"] = True

    with flask_app.app.test_client() as client:
        yield client

    # Ensure bot registry is clean after each test.
    flask_app._bots.clear()


@pytest.fixture()
def client_with_trade(app_client, tmp_path):
    """app_client with one closed LONG trade for BTC-USDT pre-seeded."""
    tid = database.open_trade(
        symbol="BTC-USDT",
        direction="LONG",
        entry_price=40000.0,
        size=0.001,
        sl_price=39000.0,
        tp_price=41600.0,
        leverage=5,
    )
    database.close_trade(tid, exit_price=41600.0, pnl=1.6)
    return app_client


# ── GET /api/symbols ──────────────────────────────────────────────────────────

class TestApiSymbols:
    def test_returns_200_and_list(self, app_client):
        resp = app_client.get("/api/symbols")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_includes_btc_usdt(self, app_client):
        data = app_client.get("/api/symbols").get_json()
        assert "BTC-USDT" in data

    def test_includes_all_supported_symbols(self, app_client):
        import config
        data = app_client.get("/api/symbols").get_json()
        for sym in config.SUPPORTED_SYMBOLS:
            assert sym in data, f"{sym} missing from /api/symbols"

    def test_returns_non_empty_list(self, app_client):
        data = app_client.get("/api/symbols").get_json()
        assert len(data) > 0


# ── GET /api/status ───────────────────────────────────────────────────────────

class TestApiStatus:
    def test_all_status_returns_dict(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_all_status_keyed_by_symbol(self, app_client):
        import config
        data = app_client.get("/api/status").get_json()
        for sym in config.SUPPORTED_SYMBOLS:
            assert sym in data, f"{sym} missing from status dict"

    def test_all_status_entry_has_running_field(self, app_client):
        data = app_client.get("/api/status").get_json()
        for sym, status in data.items():
            assert "running" in status, f"{sym} status missing 'running'"
            assert status["running"] in (0, 1)

    def test_all_status_includes_trading_mode(self, app_client):
        import config
        data = app_client.get("/api/status").get_json()
        for sym, status in data.items():
            assert "trading_mode" in status
            assert status["trading_mode"] == config.TRADING_MODE

    def test_single_symbol_status_returns_dict(self, app_client):
        resp = app_client.get("/api/status?symbol=BTC-USDT")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert data["symbol"] == "BTC-USDT"

    def test_single_symbol_status_has_required_fields(self, app_client):
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        for field in ("symbol", "running", "trading_mode", "last_signal"):
            assert field in data, f"Missing field: {field}"

    def test_single_symbol_running_reflects_bot_state(self, app_client):
        """running=0 when bot hasn't been started."""
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert data["running"] == 0

    def test_single_symbol_running_true_when_bot_started(self, app_client):
        """running=1 is reflected when a bot is marked as running."""
        import app as flask_app
        mock_bot = MagicMock()
        mock_bot.is_running = True
        flask_app._bots["BTC-USDT"] = mock_bot

        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert data["running"] == 1

    def test_all_symbols_status_running_any(self, app_client):
        """any-running check works correctly for the global header indicator."""
        import app as flask_app
        mock_bot = MagicMock()
        mock_bot.is_running = True
        flask_app._bots["ETH-USDT"] = mock_bot

        data = app_client.get("/api/status").get_json()
        any_running = any(s["running"] == 1 for s in data.values())
        assert any_running is True


# ── GET /api/trades ───────────────────────────────────────────────────────────

class TestApiTrades:
    def test_empty_returns_empty_list(self, app_client):
        resp = app_client.get("/api/trades")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_list_of_trades(self, client_with_trade):
        data = client_with_trade.get("/api/trades").get_json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_trade_has_required_fields(self, client_with_trade):
        """Frontend renders: id, symbol, direction, entry_price, exit_price,
        size, sl_price, tp_price, pnl, status, opened_at."""
        trade = client_with_trade.get("/api/trades").get_json()[0]
        for field in ("id", "symbol", "direction", "entry_price", "exit_price",
                      "size", "sl_price", "tp_price", "pnl", "status", "opened_at"):
            assert field in trade, f"Missing field: {field}"

    def test_trade_values_are_correct(self, client_with_trade):
        trade = client_with_trade.get("/api/trades").get_json()[0]
        assert trade["symbol"] == "BTC-USDT"
        assert trade["direction"] == "LONG"
        assert float(trade["entry_price"]) == pytest.approx(40000.0)
        assert float(trade["exit_price"]) == pytest.approx(41600.0)
        assert trade["status"] == "CLOSED"

    def test_limit_param_caps_results(self, app_client):
        """Frontend calls /api/trades?limit=50 – limit must be honoured."""
        for i in range(10):
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0 + i, 0.001,
                                      39000.0, 41600.0, 5)
            database.close_trade(tid, 41600.0, float(i))

        data = app_client.get("/api/trades?limit=3").get_json()
        assert len(data) == 3

    def test_symbol_filter_only_returns_matching_symbol(self, app_client):
        tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        database.close_trade(tid, 41000.0, 1.0)
        tid = database.open_trade("ETH-USDT", "SHORT", 2000.0, 0.1, 2100.0, 1900.0, 5)
        database.close_trade(tid, 1900.0, 0.5)

        btc = app_client.get("/api/trades?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/trades?symbol=ETH-USDT").get_json()

        assert all(t["symbol"] == "BTC-USDT" for t in btc)
        assert all(t["symbol"] == "ETH-USDT" for t in eth)

    def test_trades_returned_newest_first(self, app_client):
        """Frontend expects newest-first order for the trade table."""
        for price in [40000.0, 41000.0, 42000.0]:
            tid = database.open_trade("BTC-USDT", "LONG", price, 0.001, price - 1000, price + 1000, 5)
            database.close_trade(tid, price + 1000, 1.0)

        data = app_client.get("/api/trades").get_json()
        prices = [t["entry_price"] for t in data]
        assert prices == sorted(prices, reverse=True)

    def test_open_trade_included_in_history(self, app_client):
        """Open (not yet closed) trades must still appear in history."""
        database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        data = app_client.get("/api/trades").get_json()
        assert len(data) == 1
        assert data[0]["status"] == "OPEN"


# ── GET /api/trades/open ──────────────────────────────────────────────────────

class TestApiOpenTrades:
    def test_empty_when_no_open_trades(self, app_client):
        resp = app_client.get("/api/trades/open")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_closed_trades_not_returned(self, client_with_trade):
        """After closing a trade, /api/trades/open must return empty."""
        data = client_with_trade.get("/api/trades/open").get_json()
        assert data == []

    def test_open_trade_appears(self, app_client):
        database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        data = app_client.get("/api/trades/open").get_json()
        assert len(data) == 1
        assert data[0]["status"] == "OPEN"

    def test_symbol_filter_works_for_open_trades(self, app_client):
        database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        database.open_trade("ETH-USDT", "SHORT", 2000.0, 0.1, 2100.0, 1900.0, 5)

        btc = app_client.get("/api/trades/open?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/trades/open?symbol=ETH-USDT").get_json()

        assert len(btc) == 1 and btc[0]["symbol"] == "BTC-USDT"
        assert len(eth) == 1 and eth[0]["symbol"] == "ETH-USDT"


# ── GET /api/stats ────────────────────────────────────────────────────────────

class TestApiStats:
    def test_empty_db_returns_valid_structure(self, app_client):
        """Frontend depends on total, win_rate, total_pnl — must not crash on empty DB."""
        resp = app_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total" in data
        assert "win_rate" in data
        assert "total_pnl" in data

    def test_stats_has_all_fields_frontend_uses(self, app_client):
        data = app_client.get("/api/stats").get_json()
        for field in ("total", "wins", "losses", "win_rate", "total_pnl", "avg_pnl",
                      "best_trade", "worst_trade"):
            assert field in data, f"Missing stats field: {field}"

    def test_stats_correct_after_trades(self, app_client):
        """2 wins + 1 loss → total=3, win_rate≈66.67%."""
        for pnl, exit_price in [(5.0, 41600.0), (3.0, 41200.0), (-2.0, 39000.0)]:
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
            database.close_trade(tid, exit_price, pnl)

        data = app_client.get("/api/stats").get_json()
        assert data["total"] == 3
        assert data["wins"] == 2
        assert data["losses"] == 1
        assert abs(data["win_rate"] - 66.67) < 0.1
        assert abs(data["total_pnl"] - 6.0) < 0.01

    def test_per_symbol_stats_filters_correctly(self, app_client):
        tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        database.close_trade(tid, 41600.0, 5.0)
        tid = database.open_trade("ETH-USDT", "SHORT", 2000.0, 0.1, 2100.0, 1900.0, 5)
        database.close_trade(tid, 1900.0, 2.0)

        btc = app_client.get("/api/stats?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/stats?symbol=ETH-USDT").get_json()
        all_stats = app_client.get("/api/stats").get_json()

        assert btc["total"] == 1
        assert eth["total"] == 1
        assert all_stats["total"] == 2

    def test_per_symbol_stats_pnl_accuracy(self, app_client):
        tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
        database.close_trade(tid, 41600.0, 7.5)

        data = app_client.get("/api/stats?symbol=BTC-USDT").get_json()
        assert abs(data["total_pnl"] - 7.5) < 0.01

    def test_win_rate_100_percent_all_wins(self, app_client):
        for _ in range(3):
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
            database.close_trade(tid, 41600.0, 5.0)
        data = app_client.get("/api/stats").get_json()
        assert data["win_rate"] == 100.0

    def test_win_rate_zero_percent_all_losses(self, app_client):
        for _ in range(2):
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
            database.close_trade(tid, 39000.0, -5.0)
        data = app_client.get("/api/stats").get_json()
        assert data["win_rate"] == 0.0


# ── GET /api/config ───────────────────────────────────────────────────────────

class TestApiConfig:
    def test_returns_200(self, app_client):
        resp = app_client.get("/api/config")
        assert resp.status_code == 200

    def test_has_all_fields_frontend_uses(self, app_client):
        """loadConfig() in app.js renders all these fields."""
        data = app_client.get("/api/config").get_json()
        for field in ("symbol", "trading_mode", "timeframe", "leverage",
                      "risk_per_trade_pct", "stop_loss_pct", "take_profit_pct",
                      "fast_ema", "slow_ema", "trend_ema",
                      "rsi_period", "rsi_oversold", "rsi_overbought", "volume_sma_period"):
            assert field in data, f"Missing config field: {field}"

    def test_per_symbol_config_returns_correct_symbol(self, app_client):
        data = app_client.get("/api/config?symbol=ETH-USDT").get_json()
        assert data["symbol"] == "ETH-USDT"

    def test_eth_has_tighter_sl_than_btc(self, app_client):
        """ETH uses stop_loss_pct=1.5% vs BTC's 2.5%."""
        btc = app_client.get("/api/config?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/config?symbol=ETH-USDT").get_json()
        assert eth["stop_loss_pct"] < btc["stop_loss_pct"]

    def test_eth_has_wider_tp_than_btc(self, app_client):
        """ETH uses take_profit_pct=7.0% vs BTC's 4.0%."""
        btc = app_client.get("/api/config?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/config?symbol=ETH-USDT").get_json()
        assert eth["take_profit_pct"] > btc["take_profit_pct"]

    def test_config_includes_supported_symbols(self, app_client):
        import config
        data = app_client.get("/api/config").get_json()
        assert "supported_symbols" in data
        assert set(config.SUPPORTED_SYMBOLS).issubset(set(data["supported_symbols"]))

    def test_leverage_is_positive_integer(self, app_client):
        data = app_client.get("/api/config").get_json()
        assert isinstance(data["leverage"], int)
        assert data["leverage"] > 0

    def test_ema_ordering_preserved(self, app_client):
        """fast_ema < slow_ema < trend_ema – the frontend uses these for chart rendering."""
        data = app_client.get("/api/config").get_json()
        assert data["fast_ema"] < data["slow_ema"] < data["trend_ema"]


# ── GET /api/logs ─────────────────────────────────────────────────────────────

class TestApiLogsGet:
    """Basic GET /api/logs tests – detailed tests live in test_api_logs.py."""

    def test_empty_returns_list(self, app_client):
        assert app_client.get("/api/logs").get_json() == []

    def test_log_levels_preserved(self, app_client):
        """Frontend uses 'level' to colour-code log entries."""
        database.log_event("info msg", level="INFO")
        database.log_event("warn msg", level="WARNING")
        database.log_event("err msg", level="ERROR")

        data = app_client.get("/api/logs?limit=10").get_json()
        levels = {e["message"]: e["level"] for e in data}
        assert levels["info msg"] == "INFO"
        assert levels["warn msg"] == "WARNING"
        assert levels["err msg"] == "ERROR"

    def test_timestamp_field_present(self, app_client):
        database.log_event("timestamped")
        entry = app_client.get("/api/logs?limit=1").get_json()[0]
        assert "ts" in entry
        assert entry["ts"]  # non-empty


# ── POST /api/logs/clear ──────────────────────────────────────────────────────

class TestApiLogsClear:
    def test_clear_returns_ok_true(self, app_client):
        resp = app_client.post("/api/logs/clear")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_clear_removes_all_entries(self, app_client):
        for i in range(5):
            database.log_event(f"event {i}")

        app_client.post("/api/logs/clear")
        data = app_client.get("/api/logs").get_json()
        assert data == []

    def test_clear_on_empty_log_is_idempotent(self, app_client):
        """Clearing an already empty log must not error."""
        resp = app_client.post("/api/logs/clear")
        assert resp.status_code == 200
        resp2 = app_client.post("/api/logs/clear")
        assert resp2.status_code == 200

    def test_new_entries_after_clear_appear(self, app_client):
        database.log_event("before clear")
        app_client.post("/api/logs/clear")
        database.log_event("after clear")

        data = app_client.get("/api/logs").get_json()
        assert len(data) == 1
        assert data[0]["message"] == "after clear"


# ── POST /api/bot/start ───────────────────────────────────────────────────────

class TestApiBotStart:
    def test_start_all_returns_ok(self, app_client):
        """Starting all bots returns ok=True and started list."""
        import app as flask_app
        # Replace TradingBot with a mock in the registry lookup.
        with patch.object(flask_app, "_get_bot") as mock_get:
            mock_bot = MagicMock()
            mock_bot.is_running = False
            mock_get.return_value = mock_bot
            with patch.object(flask_app, "_all_bots", return_value=[mock_bot]):
                # Use direct registry manipulation instead
                pass

        import config
        for sym in config.SUPPORTED_SYMBOLS:
            mock = MagicMock()
            mock.is_running = False
            flask_app._bots[sym] = mock

        resp = app_client.post("/api/bot/start")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_start_single_symbol_returns_ok(self, app_client):
        import app as flask_app
        import config

        mock = MagicMock()
        mock.is_running = False
        flask_app._bots["BTC-USDT"] = mock

        resp = app_client.post("/api/bot/start?symbol=BTC-USDT")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        mock.start.assert_called_once()

    def test_start_calls_bot_start_method(self, app_client):
        import app as flask_app
        mock = MagicMock()
        mock.is_running = False
        flask_app._bots["ETH-USDT"] = mock

        app_client.post("/api/bot/start?symbol=ETH-USDT")
        mock.start.assert_called_once()

    def test_start_invalid_symbol_returns_400(self, app_client):
        resp = app_client.post("/api/bot/start?symbol=FAKE-USDT")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_start_already_running_returns_400(self, app_client):
        import app as flask_app
        mock = MagicMock()
        mock.is_running = True
        flask_app._bots["BTC-USDT"] = mock

        resp = app_client.post("/api/bot/start?symbol=BTC-USDT")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "already running" in data["message"].lower()

    def test_start_all_when_all_running_returns_400(self, app_client):
        import app as flask_app
        import config
        for sym in config.SUPPORTED_SYMBOLS:
            mock = MagicMock()
            mock.is_running = True
            flask_app._bots[sym] = mock

        resp = app_client.post("/api/bot/start")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False


# ── POST /api/bot/stop ────────────────────────────────────────────────────────

class TestApiBotStop:
    def test_stop_single_running_bot_returns_ok(self, app_client):
        import app as flask_app
        mock = MagicMock()
        mock.is_running = True
        flask_app._bots["BTC-USDT"] = mock

        resp = app_client.post("/api/bot/stop?symbol=BTC-USDT")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        mock.stop.assert_called_once()

    def test_stop_calls_bot_stop_method(self, app_client):
        import app as flask_app
        mock = MagicMock()
        mock.is_running = True
        flask_app._bots["ETH-USDT"] = mock

        app_client.post("/api/bot/stop?symbol=ETH-USDT")
        mock.stop.assert_called_once()

    def test_stop_invalid_symbol_returns_400(self, app_client):
        resp = app_client.post("/api/bot/stop?symbol=FAKE-USDT")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_stop_not_running_bot_returns_400(self, app_client):
        resp = app_client.post("/api/bot/stop?symbol=BTC-USDT")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "not running" in data["message"].lower()

    def test_stop_all_when_none_running_returns_400(self, app_client):
        resp = app_client.post("/api/bot/stop")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_stop_all_running_bots(self, app_client):
        import app as flask_app
        import config
        mocks = {}
        for sym in config.SUPPORTED_SYMBOLS:
            m = MagicMock()
            m.is_running = True
            flask_app._bots[sym] = m
            mocks[sym] = m

        resp = app_client.post("/api/bot/stop")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        for sym, mock in mocks.items():
            mock.stop.assert_called_once()


# ── GET /api/market/context ───────────────────────────────────────────────────

def _make_fake_candles(n: int = 120):
    """Return minimal BloFin-style candle rows (7-element lists)."""
    import time
    now_ms = int(time.time() * 1000)
    rows = []
    for i in range(n):
        ts = now_ms - (n - i) * 15 * 60 * 1000
        price = 40000.0 + i * 10
        rows.append([str(ts), str(price), str(price * 1.001),
                     str(price * 0.999), str(price + 5), "500.0", "20000000.0"])
    return rows


class TestApiMarketContext:
    def test_mocked_exchange_returns_200_and_ok(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(150)
            resp = app_client.get("/api/market/context?symbol=BTC-USDT")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_response_has_required_keys(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(150)
            data = app_client.get("/api/market/context?symbol=BTC-USDT").get_json()

        for key in ("ok", "symbol", "timeframe", "candles",
                    "diagnostics", "long_checks", "short_checks", "values"):
            assert key in data, f"Missing key: {key}"

    def test_candles_have_required_fields(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(150)
            data = app_client.get("/api/market/context?symbol=BTC-USDT").get_json()

        for candle in data["candles"]:
            for field in ("ts", "close", "ema_fast", "ema_slow", "ema_trend", "ema_200"):
                assert field in candle, f"Candle missing field: {field}"

    def test_limit_parameter_caps_candle_count(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            data = app_client.get("/api/market/context?symbol=BTC-USDT&limit=80").get_json()

        assert len(data["candles"]) <= 80

    def test_target_band_present_when_price_available(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            data = app_client.get("/api/market/context?symbol=BTC-USDT").get_json()

        assert "target_band" in data
        if data["target_band"]:
            assert "long" in data["target_band"]
            assert "short" in data["target_band"]
            assert "low" in data["target_band"]["long"]
            assert "high" in data["target_band"]["long"]

    def test_exchange_failure_returns_502(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = []
            resp = app_client.get("/api/market/context?symbol=BTC-USDT")
        assert resp.status_code == 502
        assert resp.get_json()["ok"] is False

    def test_short_candle_payload_returns_502(self, app_client):
        """Candles with fewer than 7 elements are rejected gracefully."""
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = [
                ["1234567890000", "40000"]  # only 2 fields
            ]
            resp = app_client.get("/api/market/context?symbol=BTC-USDT")
        assert resp.status_code == 502

    def test_invalid_limit_returns_400(self, app_client):
        resp = app_client.get("/api/market/context?limit=notanumber")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_limit_below_60_clamped_to_60(self, app_client):
        """Limits below 60 are silently clamped – should not error."""
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            resp = app_client.get("/api/market/context?symbol=BTC-USDT&limit=10")
        assert resp.status_code == 200

    def test_limit_above_200_clamped_to_200(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            data = app_client.get(
                "/api/market/context?symbol=BTC-USDT&limit=999"
            ).get_json()
        assert len(data["candles"]) <= 200

    def test_symbol_in_response_matches_request(self, app_client):
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(150)
            data = app_client.get("/api/market/context?symbol=ETH-USDT").get_json()
        assert data["symbol"] == "ETH-USDT"

    def test_long_and_short_checks_are_dicts(self, app_client):
        """Frontend iterates Object.entries(long_checks) and Object.entries(short_checks)."""
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            data = app_client.get("/api/market/context?symbol=BTC-USDT").get_json()
        assert isinstance(data["long_checks"], dict)
        assert isinstance(data["short_checks"], dict)

    def test_diagnostics_not_stuck_when_display_limit_small(self, app_client):
        """
        Regression: when the frontend requests a small display limit (e.g. 50),
        the endpoint must still fetch MIN_BARS_REQUIRED candles internally so
        get_signal_diagnostics() does not permanently return
        "Collecting candles (60/200)".

        The mock returns 200 candles regardless of the requested limit, which
        mirrors the real exchange.  With 200 rows the diagnostics engine should
        move past the data-collection guard and produce a meaningful status
        (anything other than the collecting-candles message).
        """
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            data = app_client.get(
                "/api/market/context?symbol=BTC-USDT&limit=50"
            ).get_json()

        assert data["ok"] is True
        # Chart candles are still capped at the display limit (clamped to 60).
        assert len(data["candles"]) <= 60
        # Diagnostics must NOT be stuck on the data-collection guard.
        waiting = data["diagnostics"].get("waiting_for", "")
        assert "Collecting candles" not in waiting, (
            f"Diagnostics still show data-collection phase with 200 candles: {waiting!r}"
        )

    def test_fetch_limit_uses_min_bars_required(self, app_client):
        """
        Regression: the exchange must be called with at least MIN_BARS_REQUIRED
        candles even when the display limit is lower, so diagnostics are accurate.
        """
        with patch("app.BloFinClient") as MockClient:
            MockClient.return_value.get_candles.return_value = _make_fake_candles(200)
            app_client.get("/api/market/context?symbol=BTC-USDT&limit=50")

        call_kwargs = MockClient.return_value.get_candles.call_args
        # Third positional arg or 'limit' keyword arg holds the fetch limit.
        actual_limit = (
            call_kwargs.kwargs.get("limit")
            if call_kwargs.kwargs.get("limit") is not None
            else call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        )
        assert actual_limit is not None, "get_candles was not called with a limit argument"
        assert actual_limit >= 200, (
            f"get_candles called with limit={actual_limit}, expected ≥ 200"
        )


# ── Static file serving ───────────────────────────────────────────────────────

class TestStaticFiles:
    def test_index_returns_200(self, app_client):
        """GET / serves index.html."""
        resp = app_client.get("/")
        assert resp.status_code == 200

    def test_index_content_type_html(self, app_client):
        resp = app_client.get("/")
        assert "text/html" in resp.content_type

    def test_style_css_returns_200(self, app_client):
        resp = app_client.get("/style.css")
        assert resp.status_code == 200

    def test_app_js_returns_200(self, app_client):
        resp = app_client.get("/app.js")
        assert resp.status_code == 200

    def test_disallowed_extension_returns_404(self, app_client):
        """Backend only serves whitelisted static extensions."""
        resp = app_client.get("/secrets.env")
        assert resp.status_code == 404

    def test_python_file_returns_404(self, app_client):
        resp = app_client.get("/config.py")
        assert resp.status_code == 404


# ── Robustness tests ──────────────────────────────────────────────────────────

class TestRobustness:
    def test_trades_invalid_limit_returns_400(self, app_client):
        """/api/trades?limit=abc must return 400, not crash with 500."""
        resp = app_client.get("/api/trades?limit=notanumber")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_logs_invalid_limit_returns_400(self, app_client):
        """/api/logs?limit=bad must return 400, not crash with 500."""
        resp = app_client.get("/api/logs?limit=bad")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_stats_empty_db_win_rate_not_nan(self, app_client):
        """win_rate must be numeric even with zero trades."""
        data = app_client.get("/api/stats").get_json()
        import math
        assert not math.isnan(data["win_rate"])

    def test_stats_total_zero_returns_numeric_win_rate(self, app_client):
        data = app_client.get("/api/stats").get_json()
        assert isinstance(data["win_rate"], (int, float))

    def test_status_unknown_symbol_returns_defaults(self, app_client):
        """Unknown symbol returns a default status dict, not an error."""
        resp = app_client.get("/api/status?symbol=UNKNOWN-USDT")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "running" in data
        assert data["running"] == 0

    def test_start_stop_round_trip(self, app_client):
        """start → stop → status running=0 round trip works correctly."""
        import app as flask_app
        mock = MagicMock()
        mock.is_running = False
        flask_app._bots["BTC-USDT"] = mock

        r1 = app_client.post("/api/bot/start?symbol=BTC-USDT")
        assert r1.status_code == 200

        mock.is_running = True
        r2 = app_client.post("/api/bot/stop?symbol=BTC-USDT")
        assert r2.status_code == 200

        mock.is_running = False
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert data["running"] == 0

    def test_concurrent_status_requests_are_stable(self, tmp_path):
        """Concurrent GET /api/status requests must all succeed (separate clients)."""
        database.DB_PATH = tmp_path / "concurrent_status.db"
        database.init_db()

        import app as flask_app
        flask_app._bots.clear()
        flask_app.app.config["TESTING"] = True
        results = []

        def fetch():
            with flask_app.app.test_client() as client:
                resp = client.get("/api/status")
                results.append(resp.status_code)

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(code == 200 for code in results), f"Some requests failed: {results}"

    def test_concurrent_trade_history_requests(self, tmp_path):
        """Concurrent GET /api/trades requests are stable (separate clients)."""
        database.DB_PATH = tmp_path / "concurrent_trades.db"
        database.init_db()

        for i in range(5):
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0 + i, 0.001,
                                      39000.0, 41600.0, 5)
            database.close_trade(tid, 41600.0, float(i))

        import app as flask_app
        flask_app.app.config["TESTING"] = True
        results = []

        def fetch():
            with flask_app.app.test_client() as client:
                resp = client.get("/api/trades")
                results.append(resp.status_code)

        threads = [threading.Thread(target=fetch) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(code == 200 for code in results)

    def test_large_trade_history_pagination(self, app_client):
        """limit=1 on 20 trades only returns 1."""
        for i in range(20):
            tid = database.open_trade("BTC-USDT", "LONG", 40000.0 + i, 0.001,
                                      39000.0, 41600.0, 5)
            database.close_trade(tid, 41600.0, float(i))

        data = app_client.get("/api/trades?limit=1").get_json()
        assert len(data) == 1

    def test_logs_clear_then_new_entries_sequential(self, app_client):
        """Repeated clear + add cycles must remain stable."""
        for cycle in range(3):
            for i in range(5):
                database.log_event(f"cycle {cycle} event {i}")
            app_client.post("/api/logs/clear")
            data = app_client.get("/api/logs").get_json()
            assert data == [], f"Cycle {cycle}: logs not cleared"

    def test_multiple_symbols_trade_stats_isolation(self, app_client):
        """PnL for one symbol must not bleed into another symbol's stats."""
        for sym, pnl in [("BTC-USDT", 100.0), ("ETH-USDT", -50.0), ("SOL-USDT", 25.0)]:
            tid = database.open_trade(sym, "LONG", 40000.0, 0.001, 39000.0, 41600.0, 5)
            database.close_trade(tid, 41600.0, pnl)

        btc = app_client.get("/api/stats?symbol=BTC-USDT").get_json()
        eth = app_client.get("/api/stats?symbol=ETH-USDT").get_json()
        sol = app_client.get("/api/stats?symbol=SOL-USDT").get_json()

        assert abs(btc["total_pnl"] - 100.0) < 0.01
        assert abs(eth["total_pnl"] - (-50.0)) < 0.01
        assert abs(sol["total_pnl"] - 25.0) < 0.01

    def test_bot_status_updated_persists_across_requests(self, app_client):
        """Status written via database persists for subsequent API reads."""
        database.update_bot_status(
            symbol="BTC-USDT", last_signal="LONG", last_price=45000.0
        )
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert data["last_signal"] == "LONG"
        assert float(data["last_price"]) == pytest.approx(45000.0)

    def test_config_returns_consistent_data_across_calls(self, app_client):
        """Two consecutive /api/config calls must return the same data."""
        r1 = app_client.get("/api/config").get_json()
        r2 = app_client.get("/api/config").get_json()
        assert r1["symbol"] == r2["symbol"]
        assert r1["leverage"] == r2["leverage"]
        assert r1["stop_loss_pct"] == r2["stop_loss_pct"]

    def test_api_endpoint_content_type_json(self, app_client):
        """All API endpoints must return application/json."""
        endpoints = [
            "/api/symbols", "/api/status", "/api/trades",
            "/api/trades/open", "/api/stats", "/api/config", "/api/logs",
        ]
        for endpoint in endpoints:
            resp = app_client.get(endpoint)
            assert "application/json" in resp.content_type, (
                f"{endpoint} did not return JSON content-type"
            )

    def test_post_logs_clear_method_not_allowed_on_get(self, app_client):
        """GET on /api/logs/clear is intercepted by the static catch-all and returns 404."""
        resp = app_client.get("/api/logs/clear")
        # Flask's GET /<path:filename> catch-all intercepts this URL and aborts 404
        # because 'clear' has no recognised static extension.
        assert resp.status_code == 404

    def test_post_bot_start_method_not_allowed_on_get(self, app_client):
        """GET on /api/bot/start returns 404 via the static catch-all (no extension)."""
        resp = app_client.get("/api/bot/start")
        assert resp.status_code == 404

    def test_post_bot_stop_method_not_allowed_on_get(self, app_client):
        """GET on /api/bot/stop returns 404 via the static catch-all (no extension)."""
        resp = app_client.get("/api/bot/stop")
        assert resp.status_code == 404


# ── Equity display correctness ────────────────────────────────────────────────

class TestEquityDisplay:
    """Verify that /api/status exposes the equity field so the frontend
    can keep the dashboard KPI card current after trade closes."""

    def test_equity_null_by_default(self, app_client):
        """Before any bot tick equity is null (not a stale zero)."""
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        # equity should be absent or null; it must not be silently initialised to 0
        assert data.get("equity") is None or data["equity"] is None

    def test_equity_persists_after_update(self, app_client):
        """Once written, equity is returned by /api/status."""
        database.update_bot_status(symbol="BTC-USDT", equity=1234.56)
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert data["equity"] is not None
        assert abs(float(data["equity"]) - 1234.56) < 0.01

    def test_all_status_includes_equity_field(self, app_client):
        """All-symbols status dict includes an 'equity' key for each symbol."""
        data = app_client.get("/api/status").get_json()
        for sym, status in data.items():
            assert "equity" in status, f"equity missing from status for {sym}"

    def test_equity_updated_after_pnl_increases(self, app_client):
        """Equity reflects PnL: writing a higher value is visible immediately."""
        database.update_bot_status(symbol="BTC-USDT", equity=1000.0)
        database.update_bot_status(symbol="BTC-USDT", equity=1050.0)
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert abs(float(data["equity"]) - 1050.0) < 0.01

    def test_equity_updated_after_loss(self, app_client):
        """Equity can decrease (loss scenario)."""
        database.update_bot_status(symbol="BTC-USDT", equity=1000.0)
        database.update_bot_status(symbol="BTC-USDT", equity=970.0)
        data = app_client.get("/api/status?symbol=BTC-USDT").get_json()
        assert abs(float(data["equity"]) - 970.0) < 0.01

    def test_multi_symbol_equity_independent(self, app_client):
        """Per-symbol equity values do not interfere with each other."""
        database.update_bot_status(symbol="BTC-USDT", equity=2000.0)
        database.update_bot_status(symbol="ETH-USDT", equity=500.0)
        all_status = app_client.get("/api/status").get_json()
        assert abs(float(all_status["BTC-USDT"]["equity"]) - 2000.0) < 0.01
        assert abs(float(all_status["ETH-USDT"]["equity"]) - 500.0) < 0.01


# ── Frontend HTML – collapse button wiring ────────────────────────────────────

class TestCollapseButtonWiring:
    """Inline onclick handlers are blocked by Content-Security-Policy in
    Home Assistant's ingress proxy.  Collapse buttons must NOT use them."""

    def _get_index_html(self):
        from pathlib import Path
        import os
        frontend = Path(__file__).parent.parent / "frontend" / "index.html"
        return frontend.read_text(encoding="utf-8")

    def test_no_inline_onclick_on_collapse_symbols_button(self):
        """btn-collapse-symbols must not use an inline onclick attribute."""
        html = self._get_index_html()
        # Extract the btn-collapse-symbols button element (rough check)
        assert 'id="btn-collapse-symbols"' in html
        # Ensure the onclick attr is not next to this button's id
        import re
        btn_match = re.search(
            r'id="btn-collapse-symbols"[^>]*>', html
        )
        assert btn_match, "btn-collapse-symbols not found in HTML"
        assert "onclick" not in btn_match.group(0), (
            "btn-collapse-symbols must not use inline onclick (CSP violation)"
        )

    def test_no_inline_onclick_on_collapse_params_button(self):
        html = self._get_index_html()
        import re
        btn_match = re.search(r'id="btn-collapse-params"[^>]*>', html)
        assert btn_match
        assert "onclick" not in btn_match.group(0)

    def test_no_inline_onclick_on_collapse_chart_button(self):
        html = self._get_index_html()
        import re
        btn_match = re.search(r'id="btn-collapse-chart"[^>]*>', html)
        assert btn_match
        assert "onclick" not in btn_match.group(0)

    def test_toggle_panel_function_defined_in_app_js(self):
        """togglePanel must remain available in app.js."""
        from pathlib import Path
        js = (Path(__file__).parent.parent / "frontend" / "app.js").read_text(encoding="utf-8")
        assert "function togglePanel(" in js

    def test_event_listeners_registered_for_collapse_buttons(self):
        """DOMContentLoaded handler in app.js must wire up all three
        collapse buttons via addEventListener so they work under CSP."""
        from pathlib import Path
        js = (Path(__file__).parent.parent / "frontend" / "app.js").read_text(encoding="utf-8")
        assert "btn-collapse-symbols" in js
        assert "btn-collapse-params" in js
        assert "btn-collapse-chart" in js
        assert "addEventListener" in js

