# trading

## Run In Home Assistant

This repository includes a Home Assistant add-on package at `trading-bot`.

### 1) Push this repository to GitHub

Home Assistant will pull the add-on from your GitHub repository, so make sure your latest files are pushed.

### 2) Add your repository in Home Assistant

In Home Assistant:

1. Go to **Settings -> Add-ons -> Add-on Store**.
2. Open the three-dot menu and choose **Repositories**.
3. Add your repo URL: `https://github.com/iejayu8/trading`.

### 3) Install and configure the add-on

1. Open **Trading Bot** add-on.
2. Install it.
3. In **Configuration**, set:
	- `BLOFIN_API_KEY`
	- `BLOFIN_API_SECRET_B64`
	- `BLOFIN_API_PASSPHRASE`
	- `TRADING_MODE` (`papertrading` or `realtrading`)
	- `PAPER_START_EQUITY` (used only for `papertrading`)
	- Optional trading overrides like `TRADING_SYMBOL`, `TRADING_MARGIN_MODE`.

### 4) Start and access UI

1. Start the add-on.
2. Open `http://<home-assistant-host>:5000`.

The bot database persists at `/data/trading_bot.db` inside the add-on volume.