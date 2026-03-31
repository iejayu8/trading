"""
Validate credentials format for BloFin sub-accounts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))

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


print("\n" + "=" * 70)
print("CREDENTIALS VALIDATION FOR BLOFIN SUB-ACCOUNTS")
print("=" * 70)

print("\nCURRENT LOADED VALUES:")
print(f"  API Key: {_mask(config.BLOFIN_API_KEY)}")
print(f"  API Key Length: {len(config.BLOFIN_API_KEY)} chars")
print(f"  Secret (decoded): {_mask(config.get_api_secret())}")
print(f"  Secret Length: {len(config.get_api_secret())} chars")
print(f"  Passphrase: {_mask(config.BLOFIN_API_PASSPHRASE)}")
print(f"  Passphrase Length: {len(config.BLOFIN_API_PASSPHRASE)} chars")

print("\nCHECKLIST:")
checks = [
    ("API Key is 32 chars", len(config.BLOFIN_API_KEY) == 32),
    ("Secret is not empty", len(config.get_api_secret()) > 0),
    ("Passphrase is not empty", len(config.BLOFIN_API_PASSPHRASE) > 0),
    ("API Key looks valid (hex/alphanumeric)", config.BLOFIN_API_KEY.replace("-", "").replace("_", "").isalnum()),
]

for check_name, result in checks:
    print(f"  {'✓' if result else '✗'} {check_name}")

print("\nNEXT STEPS:")
print("  1. Verify credentials are from BloFin SUB-ACCOUNT API management")
print("  2. Check BloFin API docs for sub-account signature requirements")
print("  3. Test credentials on BloFin API with a simpler public endpoint first")
print("  4. If still failing, BloFin support can verify your API key format")
print("\n" + "=" * 70 + "\n")
