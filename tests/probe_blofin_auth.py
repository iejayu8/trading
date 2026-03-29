import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import config

BASE_URL = "https://openapi.blofin.com"
PATH = "/api/v1/account/balance"
URL = BASE_URL + PATH

raw_secret_env = config._SECRET_B64
try:
    decoded_secret = base64.b64decode(raw_secret_env).decode()
except Exception:
    decoded_secret = raw_secret_env

secrets = {
    "raw": raw_secret_env,
    "decoded": decoded_secret,
}


def sign_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sign_b64(secret: str, msg: str) -> str:
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


results = []

for secret_mode, secret in secrets.items():
    for include_nonce_in_msg in [False, True]:
        for sig_mode in ["hex", "b64"]:
            ts = str(int(time.time() * 1000))
            nonce = str(int(time.time() * 1000))
            msg = ts + "GET" + PATH
            if include_nonce_in_msg:
                msg += nonce

            signature = sign_hex(secret, msg) if sig_mode == "hex" else sign_b64(secret, msg)

            headers = {
                "ACCESS-KEY": config.BLOFIN_API_KEY,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": config.BLOFIN_API_PASSPHRASE,
                "ACCESS-NONCE": nonce,
                "Content-Type": "application/json",
            }

            try:
                r = requests.get(URL, headers=headers, timeout=10)
                body = r.json()
            except Exception as e:
                body = {"error": str(e)}
                r = type("X", (), {"status_code": -1})

            results.append(
                {
                    "secret_mode": secret_mode,
                    "include_nonce_in_msg": include_nonce_in_msg,
                    "sig_mode": sig_mode,
                    "status": r.status_code,
                    "code": body.get("code"),
                    "msg": body.get("msg"),
                }
            )

print(json.dumps(results, indent=2))
