"""
Test script to verify BloFin account connection.

Run with:
    pytest tests/test_blofin_connection.py -v -s
or directly:
    python tests/test_blofin_connection.py
"""

import sys
from pathlib import Path

# Add backend to path for direct script execution
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import config
    from exchange import BloFinClient
except ImportError:
    # Fallback for development
    from backend import config
    from backend.exchange import BloFinClient


def test_credentials_loaded():
    """Check if credentials are loaded from .env file."""
    print("\n" + "=" * 60)
    print("Testing credential loading...")
    print("=" * 60)
    
    api_key = config.BLOFIN_API_KEY
    secret = config.get_api_secret()
    passphrase = config.BLOFIN_API_PASSPHRASE
    
    print(f"API Key loaded: {bool(api_key)} ({len(api_key)} chars)")
    print(f"API Secret loaded: {bool(secret)} ({len(secret)} chars)")
    print(f"Passphrase loaded: {bool(passphrase)} ({len(passphrase)} chars)")
    
    assert api_key, "BLOFIN_API_KEY not configured in credentials.env"
    assert secret, "BLOFIN_API_SECRET_B64 not configured in credentials.env"
    assert passphrase, "BLOFIN_API_PASSPHRASE not configured in credentials.env"
    
    print("✓ All credentials loaded successfully")


def test_client_initialization():
    """Test that BloFinClient can be instantiated."""
    print("\n" + "=" * 60)
    print("Testing client initialization...")
    print("=" * 60)
    
    client = BloFinClient()
    print(f"Client instantiated: {client}")
    print(f"Base URL: {client.BASE_URL}")
    print("✓ Client initialized successfully")


def test_account_connection():
    """Test actual connection to BloFin API and fetch account balance."""
    print("\n" + "=" * 60)
    print("Testing account connection to BloFin API...")
    print("=" * 60)
    
    client = BloFinClient()
    
    try:
        print("Fetching account balance...")
        path = "/api/v1/account/balance"
        resp = client._get(path)
        
        print(f"\nResponse: {resp}")
        
        # Check if response indicates success
        if isinstance(resp, dict):
            code = resp.get("code")
            msg = resp.get("msg")
            data = resp.get("data")
            
            print(f"Code: {code}")
            print(f"Message: {msg}")
            print(f"Data: {data}")
            
            # BloFin returns code "0" for success
            assert code == "0", f"API error: {code} - {msg}"
            assert data is not None, "No balance data returned"
            
            if isinstance(data, dict) and data:
                print(f"Account info keys: {list(data.keys())}")
        
        print(f"\n✓ Account connection successful!")
        return resp
    
    except Exception as e:
        print(f"\n✗ Account connection failed!")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {e}")
        raise


def test_ticker_data():
    """Test fetching ticker data."""
    print("\n" + "=" * 60)
    print("Testing ticker data fetch...")
    print("=" * 60)
    
    client = BloFinClient()
    symbol = config.TRADING_SYMBOL or "BTC-USDT"
    
    try:
        print(f"Fetching ticker for {symbol}...")
        path = "/api/v1/market/tickers"
        resp = client._get(path, {"instId": symbol})
        
        print(f"Response: {resp}")
        
        # Check response code
        if isinstance(resp, dict):
            code = resp.get("code")
            msg = resp.get("msg")
            data = resp.get("data")
            
            print(f"Code: {code}")
            print(f"Message: {msg}")
            
            # BloFin returns code "0" for success
            assert code == "0", f"API error: {code} - {msg}"
            assert isinstance(data, list) and len(data) > 0, "No ticker data returned"
            
            ticker = data[0]
            print(f"✓ Ticker fetch successful!")
            print(f"Ticker data: {ticker}")
        
        return resp
    
    except Exception as e:
        print(f"✗ Ticker fetch failed!")
        print(f"Error: {e}")
        raise


def test_positions():
    """Test fetching account positions."""
    print("\n" + "=" * 60)
    print("Testing positions fetch...")
    print("=" * 60)
    
    client = BloFinClient()
    symbol = config.TRADING_SYMBOL or "BTC-USDT"
    
    try:
        print(f"Fetching positions for {symbol}...")
        path = "/api/v1/account/positions"
        resp = client._get(path, {"instId": symbol})
        
        print(f"Response: {resp}")
        
        # Check response code
        if isinstance(resp, dict):
            code = resp.get("code")
            msg = resp.get("msg")
            data = resp.get("data")
            
            print(f"Code: {code}")
            print(f"Message: {msg}")
            
            # BloFin returns code "0" for success
            assert code == "0", f"API error: {code} - {msg}"
            assert isinstance(data, list), "Positions data should be a list"
            
            print(f"✓ Positions fetch successful!")
            print(f"Number of positions: {len(data)}")
            if data:
                print(f"Positions data: {data}")
        
        return resp
    
    except Exception as e:
        print(f"✗ Positions fetch failed!")
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    # Run tests manually
    print("\nBLOFIN ACCOUNT CONNECTION TEST SUITE")
    print("=" * 60)
    
    try:
        test_credentials_loaded()
        test_client_initialization()
        test_account_connection()
        test_ticker_data()
        test_positions()
        
        print("\n" + "=" * 60)
        print("✓ ALL TESTS PASSED!")
        print("=" * 60)
    
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"✗ TEST FAILED: {e}")
        print("=" * 60)
        sys.exit(1)
