"""
Test signature with different formats to find what works.
"""

import sys
import hashlib
import hmac
import time
import base64
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


def test_signature_variations():
    """Test different signature combinations."""
    print("\n" + "=" * 70)
    print("TESTING SIGNATURE VARIATIONS")
    print("=" * 70)
    
    # Get the raw value from env
    raw_secret_b64 = config._SECRET_B64
    decoded_secret = config.get_api_secret()
    
    print(f"\nRAW SECRET (from .env): {_mask(raw_secret_b64)}")
    print(f"DECODED SECRET: {_mask(decoded_secret)}")
    
    ts = str(int(time.time() * 1000))
    nonce = str(int(time.time() * 1000000))
    method = "GET"
    path = "/api/v1/account/balance"
    body = ""
    
    print(f"\n{'='*70}")
    print("VARIATION 1: Without nonce in signature (might be for sub-accounts)")
    print("="*70)
    message1 = ts + method.upper() + path + (body or "")
    sig1 = hmac.new(decoded_secret.encode(), message1.encode(), hashlib.sha256).hexdigest()
    print(f"Message: {repr(message1)}")
    print(f"Signature: {sig1}")
    
    print(f"\n{'='*70}")
    print("VARIATION 2: With nonce in signature (current implementation)")
    print("="*70)
    message2 = ts + method.upper() + path + (body or "") + nonce
    sig2 = hmac.new(decoded_secret.encode(), message2.encode(), hashlib.sha256).hexdigest()
    print(f"Message: {repr(message2)}")
    print(f"Signature: {sig2}")
    
    print(f"\n{'='*70}")
    print("VARIATION 3: Using raw base64 secret as-is (without decoding)")
    print("="*70)
    message3 = ts + method.upper() + path + (body or "")
    sig3 = hmac.new(raw_secret_b64.encode(), message3.encode(), hashlib.sha256).hexdigest()
    print(f"Message: {repr(message3)}")
    print(f"Signature: {sig3}")
    
    print(f"\n{'='*70}")
    print("VARIATION 4: Using raw base64 + nonce")
    print("="*70)
    message4 = ts + method.upper() + path + (body or "") + nonce
    sig4 = hmac.new(raw_secret_b64.encode(), message4.encode(), hashlib.sha256).hexdigest()
    print(f"Message: {repr(message4)}")
    print(f"Signature: {sig4}")
    
    print(f"\n{'='*70}")
    print("VARIATION 5: Nonce in milliseconds, not microseconds")
    print("="*70)
    nonce_ms = str(int(time.time() * 1000))
    message5 = ts + method.upper() + path + (body or "") + nonce_ms
    sig5 = hmac.new(decoded_secret.encode(), message5.encode(), hashlib.sha256).hexdigest()
    print(f"Message: {repr(message5)}")
    print(f"Signature: {sig5}")
    
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    test_signature_variations()
