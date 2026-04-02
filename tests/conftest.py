"""
conftest.py – Shared pytest fixtures and configuration.

Pytest automatically discovers and loads this file for all tests.
"""

import sys
from pathlib import Path

import pytest

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


@pytest.fixture
def backend_path():
    """Return path to the backend source directory."""
    return project_root / "backend"


@pytest.fixture
def test_data_path():
    """Return path to test data directory."""
    return Path(__file__).parent / "test_data"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """
    Isolate environment for each test.
    Prevents test interference from shared environment variables.
    """
    # Store original values
    import os
    
    original_env = os.environ.copy()
    
    yield
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_blofin_credentials(monkeypatch):
    """Provide mock BloFin credentials for testing."""
    monkeypatch.setenv("BLOFIN_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("BLOFIN_API_SECRET_B64", "dGVzdF9zZWNyZXRfa2V5")  # base64: "test_secret_key"
    monkeypatch.setenv("BLOFIN_API_PASSPHRASE", "test_passphrase")
    monkeypatch.setenv("TRADING_SYMBOL", "BTC-USDT")
    monkeypatch.setenv("TRADING_LEVERAGE", "5")
    
    return {
        "api_key": "test_api_key_12345",
        "secret": "test_secret_key",
        "passphrase": "test_passphrase",
    }
