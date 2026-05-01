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
from exchange import BloFinClient


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
        client._session.get = MagicMock(side_effect=[mock_resp, mock_resp])
        result = client.get_candles("BTC-USDT")
        assert result == []

    def test_get_candles_uses_recent_endpoint_before_history(self, monkeypatch):
        client = _make_client(monkeypatch)
        recent = [
            [str(200 - i), "1", "1", "1", "1", "1", "1"]
            for i in range(100)
        ]
        older = [["100", "1", "1", "1", "1", "1", "1"]]
        client._session.get = MagicMock(side_effect=[
            _mock_response({"code": "0", "data": recent}),
            _mock_response({"code": "0", "data": older}),
        ])

        result = client.get_candles("BTC-USDT", "15m", 150)

        assert result == recent + older
        first_url = client._session.get.call_args_list[0][0][0]
        second_url = client._session.get.call_args_list[1][0][0]
        assert "/api/v1/market/candles" in first_url
        assert "/api/v1/market/history-candles" in second_url

    def test_get_candles_falls_back_to_history_when_recent_is_empty(self, monkeypatch):
        client = _make_client(monkeypatch)
        history = [["1700000000000", "50000", "51000", "49000", "50500", "10", "500000"]]
        client._session.get = MagicMock(side_effect=[
            _mock_response({"code": "0", "data": []}),
            _mock_response({"code": "0", "data": history}),
        ])

        result = client.get_candles("BTC-USDT", "15m", 100)

        assert result == history

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
