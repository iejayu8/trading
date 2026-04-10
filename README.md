# trading

## Developer Setup

After cloning, run the one-time setup script to activate the project's git hooks:

```sh
sh setup-hooks.sh
```

This sets `core.hooksPath = .githooks` so the **pre-commit hook** runs automatically on every `git commit`.

### What the pre-commit hook does

`backend/` and `frontend/` are the source-of-truth.  
On every commit the hook mirrors both directories into `trading-bot/backend/` and `trading-bot/frontend/` (the Home Assistant add-on copy) so the two are always in sync.  
SQLite database files (`*.db`, `*.db-shm`, `*.db-wal`) are excluded from the mirror.

---

## Backend Import Policy

Backend modules prefer package-relative imports (for example, `from . import config`).

Some workflows in this repository still execute backend modules in direct-module mode
(tests and certain local tooling), where package-relative imports are unavailable. For
that reason, selected backend files keep a narrow `ImportError` fallback to absolute
imports.

In short:

- Preferred runtime mode: `python -m backend.app`
- Supported compatibility mode: direct module imports with fallback

This keeps package execution as the standard while preserving compatibility during
migration.

## Run As Windows Desktop App

This repository also includes a desktop launcher for Windows:

1. Install dependencies:
	- `pip install -r backend/requirements.txt`
	- `pip install -r requirements_desktop.txt`
2. Run from source:
	- `python desktop_app.py`
3. Or build an executable:
	- `build_exe.bat`

Desktop-specific files are:

- `desktop_app.py`
- `build_exe.bat`
- `requirements_desktop.txt`

The desktop launcher always runs the same root application code from `backend` and `frontend`.

## Run In Home Assistant

This repository includes a Home Assistant add-on package at `trading-bot`.

The add-on package only contains Home Assistant-specific files.
The application source of truth remains in the top-level `backend` and `frontend` folders, which are copied into the add-on image during build.

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