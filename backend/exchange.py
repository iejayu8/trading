"""
exchange.py – BloFin REST API client.

Handles authentication (HMAC-SHA256) and wraps the endpoints used
by the trading bot: market data, account info, order management.
"""

import hashlib
import hmac
import json
import time
import base64
from urllib.parse import urlencode
from uuid import uuid4
from typing import Any

import requests

import config


class BloFinClient:
    """Thin wrapper around the BloFin REST API."""

    BASE_URL = "https://openapi.blofin.com"

    def __init__(self) -> None:
        self._api_key = config.BLOFIN_API_KEY
        self._secret = config.get_api_secret()
        self._passphrase = config.BLOFIN_API_PASSPHRASE
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── Authentication ────────────────────────────────────────────────────────

    def _sign(
        self,
        timestamp: str,
        method: str,
        path: str,
        nonce: str,
        body: str = "",
    ) -> str:
        """Generate BloFin signature: base64(hexdigest(HMAC_SHA256(path+method+ts+nonce+body)))."""
        prehash = path + method.upper() + timestamp + nonce + (body or "")
        hex_sig = hmac.new(
            self._secret.encode(),
            prehash.encode(),
            hashlib.sha256,
        ).hexdigest().encode()
        return base64.b64encode(hex_sig).decode()

    def _headers(self, method: str, path: str, nonce: str, body: str = "") -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, nonce, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self._passphrase,
            "ACCESS-NONCE": nonce,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = urlencode(params or {}, doseq=True)
        signed_path = f"{path}?{query}" if query else path
        url = self.BASE_URL + signed_path
        nonce = str(uuid4())
        headers = self._headers("GET", signed_path, nonce)
        resp = self._session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"))
        nonce = str(uuid4())
        headers = self._headers("POST", path, nonce, body)
        url = self.BASE_URL + path
        resp = self._session.post(url, headers=headers, data=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── Market data (public) ──────────────────────────────────────────────────

    def get_candles(
        self, symbol: str, bar: str = "15m", limit: int = 200
    ) -> list[list]:
        """
        Fetch OHLCV candlestick data.

        Returns list of [ts, open, high, low, close, vol, volCcy].
        """
        path = "/api/v1/market/candles"
        params = {"instId": symbol, "bar": bar, "limit": limit}
        resp = self._get(path, params)
        return resp.get("data", [])

    def get_ticker(self, symbol: str) -> dict:
        path = "/api/v1/market/tickers"
        resp = self._get(path, {"instId": symbol})
        data = resp.get("data", [])
        return data[0] if data else {}

    # ── Account (private) ─────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Return USDT futures account balance."""
        path = "/api/v1/account/balance"
        resp = self._get(path)
        return resp.get("data", {})

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        path = "/api/v1/account/positions"
        params = {}
        if symbol:
            params["instId"] = symbol
        resp = self._get(path, params)
        return resp.get("data", [])

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,        # "buy" | "sell"
        order_type: str,  # "market" | "limit"
        size: float,
        price: float | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
    ) -> dict:
        """Place a futures order on BloFin."""
        payload: dict[str, Any] = {
            "instId": symbol,
            "marginMode": "cross",
            "positionSide": "net",
            "side": side,
            "orderType": order_type,
            "size": str(size),
        }
        if price is not None:
            payload["price"] = str(price)
        if sl_price is not None:
            payload["slTriggerPrice"] = str(sl_price)
            payload["slOrderPrice"] = "-1"  # market sl
        if tp_price is not None:
            payload["tpTriggerPrice"] = str(tp_price)
            payload["tpOrderPrice"] = "-1"  # market tp

        return self._post("/api/v1/trade/order", payload)

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        return self._post(
            "/api/v1/trade/cancel-order",
            {"instId": symbol, "orderId": order_id},
        )

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._post(
            "/api/v1/account/set-leverage",
            {"instId": symbol, "leverage": str(leverage), "marginMode": "cross"},
        )

    def get_order_history(self, symbol: str, limit: int = 50) -> list[dict]:
        path = "/api/v1/trade/orders-history"
        resp = self._get(path, {"instId": symbol, "limit": limit})
        return resp.get("data", [])
