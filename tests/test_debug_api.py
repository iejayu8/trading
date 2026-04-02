"""
Unit tests for BloFin exchange client: signature generation and header building.

These tests are fully offline — no real API calls are made.
"""

import base64
import hashlib
import hmac
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import config
from exchange import BloFinClient


def _make_client(monkeypatch, secret: str = "test_secret") -> BloFinClient:
    """Return a BloFinClient wired with dummy credentials."""
    encoded = base64.b64encode(secret.encode()).decode()
    monkeypatch.setattr(config, "BLOFIN_API_KEY", "test_api_key")
    monkeypatch.setattr(config, "_SECRET_B64", encoded)
    monkeypatch.setattr(config, "BLOFIN_API_PASSPHRASE", "test_passphrase")
    return BloFinClient()


class TestSignatureGeneration:
    def test_sign_returns_base64_string(self, monkeypatch):
        """_sign() output is valid base64."""
        client = _make_client(monkeypatch)
        sig = client._sign("1234567890", "GET", "/api/v1/account/balance", "some-nonce")
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64  # hex-encoded SHA256 = 64 bytes

    def test_sign_is_deterministic(self, monkeypatch):
        """Same inputs always produce the same signature."""
        client = _make_client(monkeypatch)
        sig1 = client._sign("ts", "GET", "/path", "nonce", "body")
        sig2 = client._sign("ts", "GET", "/path", "nonce", "body")
        assert sig1 == sig2

    def test_sign_changes_with_different_timestamp(self, monkeypatch):
        client = _make_client(monkeypatch)
        sig1 = client._sign("1000", "GET", "/path", "nonce")
        sig2 = client._sign("2000", "GET", "/path", "nonce")
        assert sig1 != sig2

    def test_sign_changes_with_different_method(self, monkeypatch):
        client = _make_client(monkeypatch)
        sig_get  = client._sign("ts", "GET",  "/path", "nonce")
        sig_post = client._sign("ts", "POST", "/path", "nonce")
        assert sig_get != sig_post

    def test_sign_changes_with_different_path(self, monkeypatch):
        client = _make_client(monkeypatch)
        sig1 = client._sign("ts", "GET", "/api/v1/account/balance", "nonce")
        sig2 = client._sign("ts", "GET", "/api/v1/account/positions", "nonce")
        assert sig1 != sig2

    def test_sign_changes_with_different_nonce(self, monkeypatch):
        client = _make_client(monkeypatch)
        sig1 = client._sign("ts", "GET", "/path", "nonce-a")
        sig2 = client._sign("ts", "GET", "/path", "nonce-b")
        assert sig1 != sig2

    def test_sign_changes_with_different_body(self, monkeypatch):
        client = _make_client(monkeypatch)
        sig1 = client._sign("ts", "POST", "/path", "nonce", '{"side":"buy"}')
        sig2 = client._sign("ts", "POST", "/path", "nonce", '{"side":"sell"}')
        assert sig1 != sig2

    def test_sign_matches_manual_hmac(self, monkeypatch):
        """Signature matches hand-computed HMAC-SHA256 of the prehash string."""
        secret = "my_test_secret"
        client = _make_client(monkeypatch, secret=secret)
        ts, method, path, nonce, body = "1700000000000", "GET", "/api/v1/account/balance", "abc-nonce", ""
        prehash = path + method.upper() + ts + nonce + body
        expected_hex = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest().encode()
        expected_sig = base64.b64encode(expected_hex).decode()
        assert client._sign(ts, method, path, nonce, body) == expected_sig

    def test_sign_empty_body_treated_as_empty_string(self, monkeypatch):
        """None body and empty string body produce the same signature."""
        client = _make_client(monkeypatch)
        sig_none  = client._sign("ts", "GET", "/path", "nonce", "")
        sig_empty = client._sign("ts", "GET", "/path", "nonce")
        assert sig_none == sig_empty


class TestHeaderBuilding:
    def test_headers_contain_required_keys(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/api/v1/account/balance", "test-nonce")
        for key in ("ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE", "ACCESS-NONCE"):
            assert key in headers, f"Missing header: {key}"

    def test_headers_access_key_matches_config(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/path", "nonce")
        assert headers["ACCESS-KEY"] == "test_api_key"

    def test_headers_passphrase_matches_config(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/path", "nonce")
        assert headers["ACCESS-PASSPHRASE"] == "test_passphrase"

    def test_headers_nonce_matches_input(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/path", "my-unique-nonce")
        assert headers["ACCESS-NONCE"] == "my-unique-nonce"

    def test_headers_timestamp_is_numeric_string(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/path", "nonce")
        assert headers["ACCESS-TIMESTAMP"].isdigit()

    def test_headers_sign_is_base64(self, monkeypatch):
        client = _make_client(monkeypatch)
        headers = client._headers("GET", "/path", "nonce")
        # Should not raise
        base64.b64decode(headers["ACCESS-SIGN"])


class TestClientInit:
    def test_base_url_is_blofin(self, monkeypatch):
        client = _make_client(monkeypatch)
        assert "blofin.com" in client.BASE_URL

    def test_client_uses_https(self, monkeypatch):
        client = _make_client(monkeypatch)
        assert client.BASE_URL.startswith("https://")

