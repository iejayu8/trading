# Trading Bot Home Assistant Add-on

This add-on runs the BloFin trading bot and dashboard inside Home Assistant.

This folder contains only Home Assistant add-on packaging files.
The runtime application code is built from the repository root `backend` and `frontend` directories.

## What it runs

- Flask API + Web UI on port 5000
- Trading engine background thread
- SQLite database persisted in /data/trading_bot.db

## Configuration options

Set these in the add-on Configuration tab:

- BLOFIN_API_KEY
- BLOFIN_API_SECRET_B64
- BLOFIN_API_PASSPHRASE
- TRADING_MODE (papertrading or realtrading)
- PAPER_START_EQUITY (used only in papertrading)
- TRADING_SYMBOL
- TRADING_LEVERAGE
- RISK_PER_TRADE
- TRADING_MARGIN_MODE (cross or isolated)

## Notes

- Secrets are written to /app/credentials.env at container start from Home Assistant options.
- The bot database is persisted under the add-on data volume.
