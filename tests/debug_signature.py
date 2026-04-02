"""
Debug signature generation to identify the issue.
"""

import sys
import hashlib
import hmac
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import config
except ImportError:
    from backend import config


def _mask(value: str, visible: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def debug_signature():
    """Debug the signature generation step by step."""
    print("\n" + "=" * 70)
    print("SIGNATURE GENERATION DEBUG")
    print("=" * 70)
    
    api_key = config.BLOFIN_API_KEY
    secret = config.get_api_secret()
    passphrase = config.BLOFIN_API_PASSPHRASE
    
    print(f"\n1. CREDENTIALS:")
    print(f"   API Key: {_mask(api_key)}")
    print(f"   API Key Length: {len(api_key)}")
    print(f"   Secret (decoded): {_mask(secret)}")
    print(f"   Secret Length: {len(secret)}")
    print(f"   Passphrase: {_mask(passphrase)}")
    
    # Simulate signature for a GET request
    ts = str(int(time.time() * 1000))
    nonce = str(int(time.time() * 1000000))
    method = "GET"
    path = "/api/v1/account/balance"
    body = ""
    
    print(f"\n2. REQUEST DETAILS:")
    print(f"   Timestamp (ms): {ts}")
    print(f"   Nonce (µs): {nonce}")
    print(f"   Method: {method}")
    print(f"   Path: {path}")
    print(f"   Body: {repr(body)}")
    
    # Build message for signature
    message = ts + method.upper() + path + (body or "") + (nonce or "")
    print(f"\n3. SIGNATURE MESSAGE:")
    print(f"   Message: {repr(message)}")
    print(f"   Message Length: {len(message)}")
    
    # Generate signature
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    
    print(f"\n4. SIGNATURE:")
    print(f"   Signature: {signature}")
    print(f"   Signature Length: {len(signature)}")
    
    # Check if secret might be the issue
    print(f"\n5. SECRET KEY ANALYSIS:")
    print(f"   Secret decoded correctly: {bool(secret)}")
    print(f"   Secret is base64 safe: {all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in config._SECRET_B64)}")
    print(f"   Secret prefix length check (first 20 chars): {len(secret[:20])}")
    
    print(f"\n6. SUGGESTIONS FOR DEBUGGING:")
    print(f"   - Check if secret needs to remain base64 encoded (not decoded)")
    print(f"   - Check if nonce should be milliseconds instead of microseconds")
    print(f"   - Check if nonce should NOT be included in signature")
    print(f"   - Verify API key/secret are from sub-account with correct permissions")
    print(f"   - Check BloFin API docs for sub-account specific requirements")
    
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    debug_signature()
