#!/usr/bin/with-contenv bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"
CREDENTIALS_FILE="/app/credentials.env"

if [[ -f "${OPTIONS_FILE}" ]]; then
  python3 - <<'PY'
import json
from pathlib import Path

options_path = Path('/data/options.json')
credentials_path = Path('/app/credentials.env')

with options_path.open('r', encoding='utf-8') as fh:
    options = json.load(fh)

def as_str(value):
    if value is None:
        return ''
    return str(value)

keys = [
    'BLOFIN_API_KEY',
    'BLOFIN_API_SECRET_B64',
    'BLOFIN_API_PASSPHRASE',
    'TRADING_MODE',
    'PAPER_START_EQUITY',
    'TRADING_SYMBOL',
    'TRADING_LEVERAGE',
    'RISK_PER_TRADE',
    'TRADING_MARGIN_MODE',
]

with credentials_path.open('w', encoding='utf-8') as fh:
    for key in keys:
        fh.write(f"{key}={as_str(options.get(key, ''))}\n")

print('Generated /app/credentials.env from Home Assistant options')
PY
fi

export TRADING_CREDENTIALS_FILE="${CREDENTIALS_FILE}"
export TRADING_DB_PATH="/data/trading_bot.db"

cd /app/backend
exec python3 app.py
