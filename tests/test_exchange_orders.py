"""Tests for exchange.py: place_order, cancel_order, set_leverage, get_order_history.

These unit tests mock the HTTP session to avoid real API calls while
verifying correct payload construction, URL assembly, and response parsing.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


def _make_client():
    """Create a BloFinClient with mocked session for deterministic tests."""
    import config
    from exchange import BloFinClient

    client = BloFinClient()
    client._session = MagicMock()
    return client


def _mock_response(json_data, status_code=200):
    """Create a mock response object."""
    mock = MagicMock()
    mock.json.return_value = json_data
    mock.status_code = status_code
    mock.raise_for_status.return_value = None
    return mock


# ── place_order ───────────────────────────────────────────────────────────────


class TestPlaceOrder:
    def test_market_order_payload(self):
        client = _make_client()
        client._session.post.return_value = _mock_response(
            {"code": "0", "msg": "success", "data": [{"orderId": "123"}]}
        )

        result = client.place_order(
            symbol="BTC-USDT",
            side="buy",
            order_type="market",
            size=0.01,
        )

        assert result["code"] == "0"
        call_args = client._session.post.call_args
        assert "/api/v1/trade/order" in call_args[0][0]

        # Verify body contains required fields
        import json
        body = json.loads(call_args[1]["data"])
        assert body["instId"] == "BTC-USDT"
        assert body["side"] == "buy"
        assert body["orderType"] == "market"
        assert body["size"] == "0.01"
        assert body["positionSide"] == "net"

    def test_limit_order_includes_price(self):
        client = _make_client()
        client._session.post.return_value = _mock_response(
            {"code": "0", "data": [{"orderId": "456"}]}
        )

        client.place_order(
            symbol="ETH-USDT",
            side="sell",
            order_type="limit",
            size=0.5,
            price=2000.0,
        )

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["price"] == "2000.0"

    def test_sl_tp_prices_in_payload(self):
        client = _make_client()
        client._session.post.return_value = _mock_response(
            {"code": "0", "data": [{"orderId": "789"}]}
        )

        client.place_order(
            symbol="BTC-USDT",
            side="buy",
            order_type="market",
            size=0.01,
            sl_price=49000.0,
            tp_price=52000.0,
        )

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["slTriggerPrice"] == "49000.0"
        assert body["slOrderPrice"] == "-1"
        assert body["tpTriggerPrice"] == "52000.0"
        assert body["tpOrderPrice"] == "-1"

    def test_client_order_id_in_payload(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.place_order(
            symbol="BTC-USDT",
            side="buy",
            order_type="market",
            size=0.01,
            client_order_id="test-cid-123",
        )

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["clientOrderId"] == "test-cid-123"

    def test_no_optional_fields_when_none(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.place_order("BTC-USDT", "buy", "market", 0.01)

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert "price" not in body
        assert "slTriggerPrice" not in body
        assert "tpTriggerPrice" not in body
        assert "clientOrderId" not in body

    def test_sell_side_for_short(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.place_order("BTC-USDT", "sell", "market", 0.01)

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["side"] == "sell"

    def test_http_error_propagates(self):
        client = _make_client()
        resp = _mock_response({}, 500)
        resp.raise_for_status.side_effect = Exception("500 Server Error")
        client._session.post.return_value = resp

        with pytest.raises(Exception, match="500"):
            client.place_order("BTC-USDT", "buy", "market", 0.01)


# ── cancel_order ──────────────────────────────────────────────────────────────


class TestCancelOrder:
    def test_cancel_order_payload(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        result = client.cancel_order("BTC-USDT", "order-123")

        assert result["code"] == "0"
        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["instId"] == "BTC-USDT"
        assert body["orderId"] == "order-123"

    def test_cancel_order_url(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.cancel_order("ETH-USDT", "456")

        url = client._session.post.call_args[0][0]
        assert "/api/v1/trade/cancel-order" in url


# ── set_leverage ──────────────────────────────────────────────────────────────


class TestSetLeverage:
    def test_set_leverage_payload(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        result = client.set_leverage("BTC-USDT", 10)

        assert result["code"] == "0"
        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert body["instId"] == "BTC-USDT"
        assert body["leverage"] == "10"

    def test_set_leverage_url(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.set_leverage("SOL-USDT", 5)

        url = client._session.post.call_args[0][0]
        assert "/api/v1/account/set-leverage" in url

    def test_margin_mode_included(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.set_leverage("BTC-USDT", 5)

        import json
        body = json.loads(client._session.post.call_args[1]["data"])
        assert "marginMode" in body


# ── get_order_history ─────────────────────────────────────────────────────────


class TestGetOrderHistory:
    def test_returns_data_list(self):
        client = _make_client()
        client._session.get.return_value = _mock_response(
            {"code": "0", "data": [{"orderId": "1"}, {"orderId": "2"}]}
        )

        result = client.get_order_history("BTC-USDT")

        assert len(result) == 2
        assert result[0]["orderId"] == "1"

    def test_empty_response(self):
        client = _make_client()
        client._session.get.return_value = _mock_response({"code": "0", "data": []})

        result = client.get_order_history("BTC-USDT")
        assert result == []

    def test_custom_limit(self):
        client = _make_client()
        client._session.get.return_value = _mock_response({"code": "0", "data": []})

        client.get_order_history("BTC-USDT", limit=10)

        url = client._session.get.call_args[0][0]
        assert "limit=10" in url

    def test_url_path(self):
        client = _make_client()
        client._session.get.return_value = _mock_response({"code": "0", "data": []})

        client.get_order_history("ETH-USDT")

        url = client._session.get.call_args[0][0]
        assert "/api/v1/trade/orders-history" in url

    def test_no_data_key_returns_empty(self):
        client = _make_client()
        client._session.get.return_value = _mock_response({"code": "0"})

        result = client.get_order_history("BTC-USDT")
        assert result == []


# ── Authentication headers ────────────────────────────────────────────────────


class TestAuthHeaders:
    def test_post_includes_auth_headers(self):
        client = _make_client()
        client._session.post.return_value = _mock_response({"code": "0", "data": []})

        client.place_order("BTC-USDT", "buy", "market", 0.01)

        headers = client._session.post.call_args[1]["headers"]
        assert "ACCESS-KEY" in headers
        assert "ACCESS-SIGN" in headers
        assert "ACCESS-TIMESTAMP" in headers
        assert "ACCESS-PASSPHRASE" in headers
        assert "ACCESS-NONCE" in headers

    def test_get_includes_auth_headers(self):
        client = _make_client()
        client._session.get.return_value = _mock_response({"code": "0", "data": []})

        client.get_order_history("BTC-USDT")

        headers = client._session.get.call_args[1]["headers"]
        assert "ACCESS-KEY" in headers
        assert "ACCESS-SIGN" in headers
