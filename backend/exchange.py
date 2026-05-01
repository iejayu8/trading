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
        resp = self._session.get(url, headers=headers, timeout=(5, 10))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"))
        nonce = str(uuid4())
        headers = self._headers("POST", path, nonce, body)
        url = self.BASE_URL + path
        resp = self._session.post(url, headers=headers, data=body, timeout=(5, 10))
        resp.raise_for_status()
        return resp.json()

    # ── Market data (public) ──────────────────────────────────────────────────

    # Map bar-string suffixes to milliseconds so ``get_candles`` can compute
    # the ``after`` lower-bound required by the BloFin history-candles endpoint.
    _BAR_SUFFIX_MS: dict[str, int] = {
        "m": 60_000,
        "H": 3_600_000,
        "D": 86_400_000,
        "W": 604_800_000,
    }

    @staticmethod
    def _bar_to_ms(bar: str) -> int:
        """Convert a BloFin bar string (e.g. ``"15m"``, ``"4H"``) to milliseconds."""
        for suffix, ms in BloFinClient._BAR_SUFFIX_MS.items():
            if bar.endswith(suffix):
                try:
                    return int(bar[:-1]) * ms
                except ValueError:
                    pass
        return 900_000  # safe default: 15m

    def get_candles(
        self, symbol: str, bar: str = "15m", limit: int = 200
    ) -> list[list]:
        """
        Fetch OHLCV candlestick data.

        BloFin's API allows at most 100 candles per request. This method first
        reads the freshest page from the regular ``candles`` endpoint, then
        paginates older data via ``history-candles`` when the caller requests
        more than 100 bars. This avoids false empty responses from
        ``history-candles`` while still honoring the 100-candle cap.

        The ``history-candles`` endpoint requires **both** ``before`` and
        ``after`` query parameters – omitting ``after`` causes BloFin to return
        an empty result set even when data exists.  ``after`` is computed as
        ``before_ts - (batch_size × bar_ms × 2)`` to give a 2× safety window
        that accommodates minor gaps in market data.

        Returns list of [ts, open, high, low, close, vol, volCcy].
        """
        BLOFIN_MAX = 100  # confirmed hard limit from BloFin API
        live_path = "/api/v1/market/candles"
        history_path = "/api/v1/market/history-candles"
        all_candles: list[list] = []
        remaining = limit
        bar_ms = self._bar_to_ms(bar)

        def _oldest_ts(batch: list[list]) -> int | None:
            try:
                return int(batch[-1][0])
            except (IndexError, TypeError, ValueError):
                return None

        # Fetch the most recent page from the live candles endpoint first.
        first_batch_size = min(remaining, BLOFIN_MAX)
        first_params: dict[str, Any] = {
            "instId": symbol,
            "bar": bar,
            "limit": first_batch_size,
        }
        first_batch = self._get(live_path, first_params).get("data", [])
        if first_batch:
            all_candles.extend(first_batch)
            remaining -= len(first_batch)

            if remaining <= 0 or len(first_batch) < first_batch_size:
                return all_candles

            # The history endpoint returns candles strictly older than ``before``.
            before_ts = _oldest_ts(first_batch)
            if before_ts is None:
                return all_candles
        else:
            # Fall back to history pagination if the live endpoint is empty.
            before_ts = int(time.time() * 1000)

        while remaining > 0:
            batch_size = min(remaining, BLOFIN_MAX)
            # BloFin history-candles requires BOTH ``before`` and ``after`` to
            # return results.  Without ``after`` the endpoint returns an empty
            # list even when historical data is available.  Use a 2× safety
            # window so minor data gaps do not cause the page to come up short.
            after_ts = before_ts - (batch_size * bar_ms * 2)
            params: dict[str, Any] = {
                "instId": symbol,
                "bar": bar,
                "limit": batch_size,
                # Return candles with timestamp strictly older than before_ts
                "before": str(before_ts),
                "after": str(after_ts),
            }

            batch = self._get(history_path, params).get("data", [])
            if not batch:
                break

            all_candles.extend(batch)
            remaining -= len(batch)

            if len(batch) < batch_size:
                break  # fewer candles available than requested; stop paging

            # Advance cursor: oldest candle in this batch is the last element
            # (BloFin returns newest-first).  Pass its timestamp as the next
            # ``before`` value so the next page starts strictly before it.
            oldest_ts = _oldest_ts(batch)
            if oldest_ts is None or oldest_ts >= before_ts:
                break
            before_ts = oldest_ts

        return all_candles

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
