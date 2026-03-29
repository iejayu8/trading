"""
Debug test to inspect BloFin API responses in detail.

Use this to understand what the API is actually returning.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import config
    from exchange import BloFinClient
except ImportError:
    from backend import config
    from backend.exchange import BloFinClient


def test_raw_api_response():
    """Test to see the raw API response from BloFin."""
    print("\n" + "=" * 70)
    print("DEBUGGING: RAW API RESPONSE")
    print("=" * 70)
    
    client = BloFinClient()
    
    # Check credentials
    print("\n1. CREDENTIALS CHECK:")
    print(f"   API Key: {'✓' if config.BLOFIN_API_KEY else '✗'} ({len(config.BLOFIN_API_KEY)} chars)")
    print(f"   API Secret: {'✓' if config.get_api_secret() else '✗'} ({len(config.get_api_secret())} chars)")
    print(f"   Passphrase: {'✓' if config.BLOFIN_API_PASSPHRASE else '✗'} ({len(config.BLOFIN_API_PASSPHRASE)} chars)")
    
    # Get raw balance response
    print("\n2. BALANCE ENDPOINT RESPONSE:")
    try:
        path = "/api/v1/account/balance"
        resp = client._get(path)
        print(f"   Status: Success")
        print(f"   Full Response:")
        print(json.dumps(resp, indent=4))
        
        if isinstance(resp, dict):
            print(f"\n   Response Keys: {list(resp.keys())}")
            if "code" in resp:
                print(f"   Code: {resp['code']}")
            if "msg" in resp:
                print(f"   Message: {resp['msg']}")
            if "data" in resp:
                data = resp["data"]
                print(f"   Data Type: {type(data).__name__}")
                print(f"   Data Content: {data}")
    except Exception as e:
        print(f"   Status: ERROR")
        print(f"   Error: {type(e).__name__}: {e}")
    
    # Get raw positions response
    print("\n3. POSITIONS ENDPOINT RESPONSE:")
    try:
        symbol = config.TRADING_SYMBOL or "BTC-USDT"
        path = "/api/v1/account/positions"
        resp = client._get(path, {"instId": symbol})
        print(f"   Status: Success")
        print(f"   Full Response:")
        print(json.dumps(resp, indent=4))
        
        if isinstance(resp, dict):
            print(f"\n   Response Keys: {list(resp.keys())}")
            if "code" in resp:
                print(f"   Code: {resp['code']}")
            if "msg" in resp:
                print(f"   Message: {resp['msg']}")
            if "data" in resp:
                data = resp["data"]
                print(f"   Data Type: {type(data).__name__}")
                if isinstance(data, list):
                    print(f"   Number of positions: {len(data)}")
                    if data:
                        print(f"   First position: {json.dumps(data[0], indent=4)}")
    except Exception as e:
        print(f"   Status: ERROR")
        print(f"   Error: {type(e).__name__}: {e}")
    
    # Get raw ticker response
    print("\n4. TICKER ENDPOINT RESPONSE:")
    try:
        symbol = config.TRADING_SYMBOL or "BTC-USDT"
        path = "/api/v1/market/tickers"
        resp = client._get(path, {"instId": symbol})
        print(f"   Status: Success")
        print(f"   Full Response:")
        print(json.dumps(resp, indent=4))
    except Exception as e:
        print(f"   Status: ERROR")
        print(f"   Error: {type(e).__name__}: {e}")
    
    print("\n" + "=" * 70)
    print("ANALYSIS:")
    print("=" * 70)
    print("✓ If balance/positions have data → Your account is properly connected")
    print("✓ If they're empty → Check BloFin API permissions/credentials")
    print("✓ If you see error codes → Check BloFin API documentation")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    test_raw_api_response()
