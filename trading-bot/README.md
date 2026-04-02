# Trading Bot Home Assistant Add-on

This add-on runs the BloFin trading bot and dashboard inside Home Assistant.

This folder contains only Home Assistant add-on packaging files.
The runtime application code is built from the repository root `backend` and `frontend` directories.

## What it runs

- Flask API + Web UI on port 5000
- Trading engine background threads — one per supported symbol (BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, LINK-USDT)
- SQLite database persisted in /data/trading_bot.db

## Configuration options

Set these in the add-on Configuration tab:

### API credentials
- `BLOFIN_API_KEY` — BloFin API key
- `BLOFIN_API_SECRET_B64` — BloFin API secret, base64-encoded
- `BLOFIN_API_PASSPHRASE` — BloFin API passphrase

### Trading settings
- `TRADING_MODE` — `papertrading` or `realtrading`
- `PAPER_START_EQUITY` — Starting virtual equity for paper trading (default: 1000)
- `TRADING_SYMBOL` — Default symbol shown in the UI (bots run on all supported symbols regardless)
- `TRADING_LEVERAGE` — Leverage multiplier, 1–125 (default: 5)
- `RISK_PER_TRADE` — Fraction of equity risked per trade, 0.001–0.1 (default: 0.01 = 1%)
- `TRADING_MARGIN_MODE` — `isolated` or `cross` (default: isolated)

### Portfolio-level risk caps
These limits apply across all running symbol bots collectively:

- `MAX_OPEN_POSITIONS` — Maximum concurrent open positions across all symbols, 1–10 (default: 3)
- `MAX_MARGIN_USAGE_PCT` — Maximum fraction of equity used as margin, 0.01–1.0 (default: 0.40 = 40%)
- `MAX_PORTFOLIO_RISK_PCT` — Maximum fraction of equity at risk across all open stops, 0.001–0.5 (default: 0.03 = 3%)
- `MAX_SYMBOL_EXPOSURE_PCT` — Maximum notional exposure per symbol as a fraction of the cap, 0.01–1.0 (default: 0.50 = 50%)

## Notes

- Secrets are written to /app/credentials.env at container start from Home Assistant options.
- The bot database is persisted under the add-on data volume.
- Each supported symbol (BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, LINK-USDT) runs its own bot instance with individually tuned strategy parameters.
