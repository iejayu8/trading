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

# Import policy:
# Prefer package-relative imports when running as `python -m backend.app`.
# Keep absolute fallback for direct-module contexts used by tests and some tools.
try:
    from . import config
except ImportError:
    import importlib

    config = importlib.import_module("config")


# ── Bar-duration helper ───────────────────────────────────────────────────────

_BAR_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}


def _bar_to_ms(bar: str) -> int:
    """Return the duration of one candlestick bar in milliseconds.

    Falls back to 900 000 ms (15 minutes) for unrecognised bar strings.
    """
    return _BAR_MS.get(bar, 900_000)


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

    #: BloFin hard-caps every candle endpoint at 100 rows per request.
    CANDLE_BATCH: int = 100

    def get_candles(
        self, symbol: str, bar: str = "15m", limit: int = 200
    ) -> list[list]:
        """Fetch OHLCV candlestick data, paginating as needed.

        BloFin hard-caps ``/api/v1/market/candles`` at 100 rows per request.
        When *limit* > 100 this method fetches multiple pages via
        ``/api/v1/market/history-candles`` (which accepts ``before``/``after``
        cursor parameters) and stitches them together.

        Returns candles in **newest-first** order (same as the raw BloFin
        response) so the existing ``_candles_to_df`` helper can reverse them
        without changes.
        """
        bar_ms = _bar_to_ms(bar)
        batch = self.CANDLE_BATCH
        all_candles: list[list] = []

        # ``before`` is an exclusive upper-bound timestamp in milliseconds.
        # Add one bar so the most recently closed candle is always included.
        before_ms = int(time.time() * 1000) + bar_ms

        while len(all_candles) < limit:
            fetch_n = min(limit - len(all_candles), batch)
            # ``after`` (inclusive lower bound) must always be provided –
            # BloFin returns an empty list when it is omitted.  Use a window
            # that is 2× the batch size wide to guarantee *fetch_n* results.
            after_ms = before_ms - fetch_n * bar_ms * 2

            params = {
                "instId": symbol,
                "bar": bar,
                "limit": fetch_n,
                "before": str(before_ms),
                "after": str(after_ms),
            }
            resp = self._get("/api/v1/market/history-candles", params)
            page: list[list] = resp.get("data", [])

            if not page:
                break

            all_candles.extend(page)

            # Advance the cursor to before the oldest candle just received.
            oldest_ts = int(page[-1][0])
            if oldest_ts >= before_ms:
                break  # no forward progress – safety guard
            before_ms = oldest_ts

            if len(page) < fetch_n:
                break  # API has no more history

        return all_candles[:limit]

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
        client_order_id: str | None = None,
    ) -> dict:
        """Place a futures order on BloFin."""
        payload: dict[str, Any] = {
            "instId": symbol,
            "marginMode": config.TRADING_MARGIN_MODE,
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
        if client_order_id:
            payload["clientOrderId"] = client_order_id

        return self._post("/api/v1/trade/order", payload)

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        return self._post(
            "/api/v1/trade/cancel-order",
            {"instId": symbol, "orderId": order_id},
        )

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._post(
            "/api/v1/account/set-leverage",
            {
                "instId": symbol,
                "leverage": str(leverage),
                "marginMode": config.TRADING_MARGIN_MODE,
            },
        )

    def get_order_history(self, symbol: str, limit: int = 50) -> list[dict]:
        path = "/api/v1/trade/orders-history"
        resp = self._get(path, {"instId": symbol, "limit": limit})
        return resp.get("data", [])

    # ── Copy trading (public lead-trader endpoints) ───────────────────────────

    def get_copy_trader_positions(self, trader_id: str) -> list[dict]:
        """Fetch the lead trader's current open positions.

        Returns a list of position dicts.  Each dict is expected to contain:
          ``instId``   – instrument ID (e.g. "BTC-USDT")
          ``side``     – "long" or "short"
          ``pos``      – position size (may also appear as ``size`` or ``positions``)
        """
        path = "/api/v1/copytrading/lead-trader/current-order"
        resp = self._get(path, {"uniqueName": trader_id})
        return resp.get("data", []) or []

    def get_copy_trader_order_history(self, trader_id: str, limit: int = 50) -> list[dict]:
        """Fetch the lead trader's recent closed orders.

        Useful for detecting newly opened/closed trades between polls.
        Returns a list of order dicts with the same shape as
        ``get_copy_trader_positions``.
        """
        path = "/api/v1/copytrading/lead-trader/order-history"
        resp = self._get(path, {"uniqueName": trader_id, "limit": limit})
        return resp.get("data", []) or []
