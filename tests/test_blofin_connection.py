"""
Integration tests: verify live BloFin API connectivity.

These tests make real HTTP requests to the BloFin API and require valid
credentials in credentials.env.  They are marked @pytest.mark.integration
so they can be excluded in CI:

    pytest -m "not integration"
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import config
    from exchange import BloFinClient
except ImportError:
    from backend import config
    from backend.exchange import BloFinClient


# Skip the entire module when no credentials are configured.
_no_creds = not (config.BLOFIN_API_KEY and config.get_api_secret() and config.BLOFIN_API_PASSPHRASE)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.blofin,
    pytest.mark.skipif(_no_creds, reason="BloFin credentials not configured"),
]


def test_credentials_loaded():
    """Credentials are present and non-empty."""
    assert config.BLOFIN_API_KEY, "BLOFIN_API_KEY not configured"
    assert config.get_api_secret(), "BLOFIN_API_SECRET_B64 not configured"
    assert config.BLOFIN_API_PASSPHRASE, "BLOFIN_API_PASSPHRASE not configured"


def test_client_initialization():
    """BloFinClient can be instantiated."""
    client = BloFinClient()
    assert client.BASE_URL.startswith("https://")


def test_account_connection():
    """Fetch account balance — API returns code '0'."""
    client = BloFinClient()
    resp = client._get("/api/v1/account/balance")
    assert isinstance(resp, dict), f"Expected dict response, got: {type(resp)}"
    assert resp.get("code") == "0", f"API error: {resp.get('code')} – {resp.get('msg')}"
    assert resp.get("data") is not None, "No balance data returned"


def test_ticker_data():
    """Fetch ticker for the configured symbol."""
    client = BloFinClient()
    symbol = config.TRADING_SYMBOL or "BTC-USDT"
    resp = client._get("/api/v1/market/tickers", {"instId": symbol})
    assert isinstance(resp, dict)
    assert resp.get("code") == "0", f"Ticker API error: {resp.get('code')} – {resp.get('msg')}"
    data = resp.get("data")
    assert isinstance(data, list) and len(data) > 0, "No ticker data returned"


def test_positions():
    """Fetch open positions — returns a list (may be empty)."""
    client = BloFinClient()
    symbol = config.TRADING_SYMBOL or "BTC-USDT"
    resp = client._get("/api/v1/account/positions", {"instId": symbol})
    assert isinstance(resp, dict)
    assert resp.get("code") == "0", f"Positions API error: {resp.get('code')} – {resp.get('msg')}"
    assert isinstance(resp.get("data"), list), "Positions data should be a list"

