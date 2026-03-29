import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import config

BASE_URL = "https://openapi.blofin.com"
PATH = "/api/v1/account/balance"
URL = BASE_URL + PATH

raw_secret = config._SECRET_B64
try:
    decoded_secret = base64.b64decode(raw_secret).decode()
except Exception:
    decoded_secret = raw_secret

secrets = {"raw": raw_secret, "decoded": decoded_secret}


def sig_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sig_b64(secret: str, msg: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


ts_values = {
    "ms": lambda: str(int(time.time() * 1000)),
    "s": lambda: str(int(time.time())),
    "iso": lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
}

nonce_values = {
    "ms": lambda: str(int(time.time() * 1000)),
    "us": lambda: str(int(time.time() * 1000000)),
}

# message builders
# t=timestamp, n=nonce, m=method, p=path, b=body
msg_patterns = {
    "t+m+p+b": lambda t, n, m, p, b: t + m + p + b,
    "t+m+p+b+n": lambda t, n, m, p, b: t + m + p + b + n,
    "t+n+m+p+b": lambda t, n, m, p, b: t + n + m + p + b,
    "n+t+m+p+b": lambda t, n, m, p, b: n + t + m + p + b,
    "m+p+t+n": lambda t, n, m, p, b: m + p + t + n,
}

results = []

for secret_name, secret in secrets.items():
    for ts_name, ts_fn in ts_values.items():
        for nonce_name, nonce_fn in nonce_values.items():
            for pattern_name, pattern_fn in msg_patterns.items():
                for enc_name, enc_fn in {"hex": sig_hex, "b64": sig_b64}.items():
                    t = ts_fn()
                    n = nonce_fn()
                    m = "GET"
                    b = ""
                    msg = pattern_fn(t, n, m, PATH, b)
                    sign = enc_fn(secret, msg)

                    headers = {
                        "ACCESS-KEY": config.BLOFIN_API_KEY,
                        "ACCESS-SIGN": sign,
                        "ACCESS-TIMESTAMP": t,
                        "ACCESS-PASSPHRASE": config.BLOFIN_API_PASSPHRASE,
                        "ACCESS-NONCE": n,
                        "Content-Type": "application/json",
                    }
                    try:
                        r = requests.get(URL, headers=headers, timeout=10)
                        body = r.json()
                        code = body.get("code")
                        msg_resp = body.get("msg")
                    except Exception as e:
                        code = "EXC"
                        msg_resp = str(e)
                        r = type("X", (), {"status_code": -1})

                    item = {
                        "secret": secret_name,
                        "ts": ts_name,
                        "nonce": nonce_name,
                        "pattern": pattern_name,
                        "enc": enc_name,
                        "status": r.status_code,
                        "code": code,
                        "msg": msg_resp,
                    }
                    results.append(item)

                    if code == "0":
                        print("FOUND SUCCESS:")
                        print(json.dumps(item, indent=2))
                        raise SystemExit(0)

# print compact summary counts
summary = {}
for r in results:
    key = (r["code"], r["msg"])
    summary[key] = summary.get(key, 0) + 1

print("No success combination found.")
print("Summary:")
print(json.dumps([{"code": k[0], "msg": k[1], "count": v} for k, v in summary.items()], indent=2))
