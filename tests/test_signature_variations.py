"""
Unit tests for BloFin exchange client HTTP methods.

All tests are offline — network calls are mocked.
"""

import json
import sys
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
from exchange import BloFinClient, _bar_to_ms


def _make_client(monkeypatch, secret: str = "test_secret") -> BloFinClient:
    encoded = base64.b64encode(secret.encode()).decode()
    monkeypatch.setattr(config, "BLOFIN_API_KEY", "test_key")
    monkeypatch.setattr(config, "_SECRET_B64", encoded)
    monkeypatch.setattr(config, "BLOFIN_API_PASSPHRASE", "test_pass")
    return BloFinClient()


def _mock_response(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


class TestGetMethod:
    def test_get_calls_session_get(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": []})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client._get("/api/v1/market/candles", {"instId": "BTC-USDT"})
        assert client._session.get.called

    def test_get_appends_query_string_to_url(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": []})
        client._session.get = MagicMock(return_value=mock_resp)
        client._get("/api/v1/market/candles", {"instId": "BTC-USDT", "bar": "15m"})
        url_called = client._session.get.call_args[0][0]
        assert "instId=BTC-USDT" in url_called
        assert "bar=15m" in url_called

    def test_get_no_params_no_question_mark(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": {}})
        client._session.get = MagicMock(return_value=mock_resp)
        client._get("/api/v1/account/balance")
        url_called = client._session.get.call_args[0][0]
        assert url_called.endswith("/api/v1/account/balance")

    def test_get_returns_json(self, monkeypatch):
        client = _make_client(monkeypatch)
        payload = {"code": "0", "data": [{"price": "50000"}]}
        mock_resp = _mock_response(payload)
        client._session.get = MagicMock(return_value=mock_resp)
        result = client._get("/api/v1/market/tickers")
        assert result == payload

    def test_get_signed_path_includes_query_in_signature(self, monkeypatch):
        """The signed path (used in signature) includes the query string."""
        client = _make_client(monkeypatch)
        signed_paths = []
        original_headers = client._headers

        def capture_headers(method, path, nonce, body=""):
            signed_paths.append(path)
            return original_headers(method, path, nonce, body)

        client._headers = capture_headers
        mock_resp = _mock_response({"code": "0", "data": []})
        client._session.get = MagicMock(return_value=mock_resp)
        client._get("/api/v1/market/candles", {"instId": "ETH-USDT"})
        assert any("instId=ETH-USDT" in p for p in signed_paths)


class TestPostMethod:
    def test_post_calls_session_post(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": [{"orderId": "99"}]})
        client._session.post = MagicMock(return_value=mock_resp)
        result = client._post("/api/v1/trade/order", {"instId": "BTC-USDT", "side": "buy"})
        assert client._session.post.called

    def test_post_sends_json_body(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0"})
        client._session.post = MagicMock(return_value=mock_resp)
        payload = {"instId": "BTC-USDT", "side": "buy", "sz": "0.001"}
        client._post("/api/v1/trade/order", payload)
        body_sent = client._session.post.call_args[1]["data"]
        assert json.loads(body_sent) == payload

    def test_post_returns_json(self, monkeypatch):
        client = _make_client(monkeypatch)
        resp_data = {"code": "0", "data": [{"orderId": "42"}]}
        mock_resp = _mock_response(resp_data)
        client._session.post = MagicMock(return_value=mock_resp)
        result = client._post("/api/v1/trade/order", {})
        assert result == resp_data


class TestPublicMethods:
    def test_get_candles_returns_data_list(self, monkeypatch):
        client = _make_client(monkeypatch)
        candles = [["1700000000000", "50000", "51000", "49000", "50500", "10", "500000"]]
        mock_resp = _mock_response({"code": "0", "data": candles})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_candles("BTC-USDT", "15m", 100)
        assert result == candles

    def test_get_candles_empty_on_no_data(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0"})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_candles("BTC-USDT")
        assert result == []

    def test_get_candles_paginates_to_reach_limit(self, monkeypatch):
        """get_candles must paginate when limit > CANDLE_BATCH (100)."""
        client = _make_client(monkeypatch)

        bar_ms = 900_000  # 15m in ms
        # Build two pages of 100 candles each (newest-first within each page).
        # Page 1: timestamps 200 .. 101 (most recent)
        page1 = [
            [str(1_700_000_000_000 + i * bar_ms), "50000", "51000", "49000", "50500", "10", "0"]
            for i in range(200, 100, -1)
        ]
        # Page 2: timestamps 100 .. 1 (older)
        page2 = [
            [str(1_700_000_000_000 + i * bar_ms), "50000", "51000", "49000", "50500", "10", "0"]
            for i in range(100, 0, -1)
        ]

        call_count = {"n": 0}

        def mock_get_side_effect(url, **kwargs):
            call_count["n"] += 1
            page = page1 if call_count["n"] == 1 else page2
            return _mock_response({"code": "0", "data": page})

        client._session.get = MagicMock(side_effect=mock_get_side_effect)
        result = client.get_candles("BTC-USDT", "15m", 200)

        assert len(result) == 200
        assert call_count["n"] == 2  # two HTTP pages were fetched

    def test_get_candles_uses_history_endpoint(self, monkeypatch):
        """get_candles must call the history-candles endpoint."""
        client = _make_client(monkeypatch)
        candles = [["1700000000000", "50000", "51000", "49000", "50500", "10", "0"]]
        mock_resp = _mock_response({"code": "0", "data": candles})
        client._session.get = MagicMock(return_value=mock_resp)
        client.get_candles("BTC-USDT", "15m", 100)
        url_called = client._session.get.call_args[0][0]
        assert "history-candles" in url_called

    def test_bar_to_ms_known_bars(self):
        assert _bar_to_ms("15m") == 900_000
        assert _bar_to_ms("1h") == 3_600_000
        assert _bar_to_ms("1d") == 86_400_000

    def test_bar_to_ms_unknown_falls_back_to_15m(self):
        assert _bar_to_ms("99x") == 900_000

    def test_get_ticker_returns_first_entry(self, monkeypatch):
        client = _make_client(monkeypatch)
        ticker = {"instId": "BTC-USDT", "last": "50000"}
        mock_resp = _mock_response({"code": "0", "data": [ticker]})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_ticker("BTC-USDT")
        assert result == ticker

    def test_get_ticker_empty_dict_when_no_data(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": []})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_ticker("BTC-USDT")
        assert result == {}

    def test_get_balance_returns_data(self, monkeypatch):
        client = _make_client(monkeypatch)
        balance = {"totalEquity": "10000"}
        mock_resp = _mock_response({"code": "0", "data": balance})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_balance()
        assert result == balance

    def test_get_positions_returns_list(self, monkeypatch):
        client = _make_client(monkeypatch)
        positions = [{"instId": "BTC-USDT", "positions": "0.1"}]
        mock_resp = _mock_response({"code": "0", "data": positions})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_positions("BTC-USDT")
        assert result == positions

    def test_get_positions_empty_when_none(self, monkeypatch):
        client = _make_client(monkeypatch)
        mock_resp = _mock_response({"code": "0", "data": []})
        client._session.get = MagicMock(return_value=mock_resp)
        result = client.get_positions("BTC-USDT")
        assert result == []

