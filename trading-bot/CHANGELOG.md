# Changelog

## 1.7.3
- feat: add exchange LED indicator to frontend header
- fix: refresh equity immediately when switching trading mode

## 1.7.2
- fix: address code review - validate version format, guard CHANGELOG newline, improve git error messages
- feat: add auto-version-bump workflow on merge to main
- fix: seed real-trading equity on bot start after mode switch

## 1.7.1
- Fix **Copy Trading toggle button doing nothing** — clicking "Copy Trading" in the strategy selector had no visible effect; `switchMode('copy')` set the local flag then called `refreshAll()` which re-fetched the server config (still `enabled: false`) and immediately overwrote the flag; extracted `updateCopyTradingUI()` helper and updated the UI directly without a server round-trip so the button, trader-ID input and status text now respond instantly

## 1.7.0
- Add **Paper / Real Trading mode toggle** in the dashboard header — switch between `papertrading` and `realtrading` at runtime without restarting the add-on (bots must be stopped first)
- New API endpoints: `GET /api/trading/mode` and `POST /api/trading/mode`
- Reorganise the mode-selector bar into two sections: **Environment** (Paper / Real Trading) and **Strategy** (Custom Strategy / Copy Trading)
- Sync `trading-bot/` add-on directory with root source — all v1.5.0–v1.6.0 features (copy trading UI, ATR-normalised MACD gate, copy trading backend) now included in the add-on

## 1.6.0
- Fix **bot not opening operations** — the MACD histogram filter used a hardcoded ±50 threshold that was only meaningful for BTC-USDT; replaced with ATR-normalised gate (`0.5 × ATR`) so the filter scales correctly to every symbol's price level and volatility
- Fix **copy trading mode cannot be disabled** — unchecking the old checkbox toggle hid the "Apply" button, making it impossible to save the disabled state once copy trading was activated; the bot would remain stuck in copy-trading mode, skipping its own strategy signals entirely
- Replace the copy trading checkbox with a proper **mode selector** (Custom Strategy / Copy Trading toggle buttons) in the dashboard header; clicking "Custom Strategy" immediately disables copy trading and saves to the database
- Fix broken `test_strategy_diagnostics.py` test file — import of removed symbol `_last_signal_bar` updated to `_last_signal_ts` (renamed in v1.4.2 cooldown fix)

## 1.5.0
- Add **Copy Trading** mode — mirror a BloFin lead trader's open positions instead of using the built-in strategy
- New toggle in the dashboard header switches between *Custom Strategy* and *Copy Trading*; entering a Trader ID / Unique Name and clicking **Apply** activates mirroring
- When copy trading is active the **Mode** KPI card shows **COPY TRADING** in purple
- Copy trading works in both paper and real trading modes; position sizing always uses your own risk parameters (`RISK_PER_TRADE`), never the lead trader's contract size
- New API endpoints: `GET /api/copytrading/config` and `POST /api/copytrading/config`
- Settings are persisted in the database so they survive bot or add-on restarts
- Environment-variable startup defaults: `COPY_TRADING_ENABLED` and `COPY_TRADING_TRADER_ID`

## 1.4.2
- Fix **signal cooldown never expiring** in production — cooldown is now tracked by candle timestamp instead of positional DataFrame index; previously `len(df) - 1` was always 199 (fixed 200-candle fetch window), so a signal fired at index 199 permanently blocked all subsequent signals

## 1.4.1
- Add **Reset Statistics** button to the dashboard that deletes all trade history, activity logs and bot status — compatible with both the standalone app and the Home Assistant add-on
- Fix equity/PnL mismatch when closing trades: the manual-close PnL formula now uses `(exit − entry) × size` directly, consistent with the bot's internal `_calc_pnl` helper and eliminating the intermediate floating-point division that could produce tiny rounding discrepancies

## 1.3.3
- Fix equity stuck at $1000 after manual trade close — `POST /api/trades/<id>/close` now immediately recalculates and persists equity in `bot_status`, matching the behaviour of the bot's internal close path

## 1.3.2
- Fix paper trading equity stuck at initial value — equity now reflects unrealised PnL from open positions in real time (updated every 5 minutes via price sync and at every candle tick)

## 1.3.1
- Fix manual close button in paper trading mode — was incorrectly calling the exchange instead of skipping it
- Fix Live Symbol Status collapse button not responding in Home Assistant ingress environment
- Auto-start all configured symbol bots on addon initialization — no manual "Start All" click required

## 1.3.0
- Add **Current Profit** column to Trade History showing live unrealised PnL (green/red) for open positions
- Add **Close** button per open position in Trade History to manually close a position at current market price
- Add dedicated 5-minute price sync thread so live prices stay accurate between 15-minute candle ticks
- Trade History: Direction, SL and TP columns now display in white instead of green/red

## 1.2.0
- Enable Home Assistant ingress integration for seamless panel embedding
- Live Symbol Status panel starts collapsed by default
- Fix paper equity displayed immediately on bot start (no longer requires waiting for the first tick)
- Fix stale `running=1` flags being left in the database after a server restart
- Fix "Stop All" when bots have orphaned database state but no live thread
- Fix unclosed SQLite connections causing occasional database lock errors

## 1.1.0
- Multi-symbol support: BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, LINK-USDT
- Per-symbol strategy parameters (stop loss %, take profit %, ADX threshold, RSI thresholds)
- Portfolio-level risk caps: max open positions, max margin usage, max portfolio risk, max symbol exposure
- SL Loss and TP Profit columns added to Trade History table
- Collapsible panels for Live Symbol Status, Strategy Parameters and Market Context

## 1.0.0
- Initial release: BloFin futures trading bot with paper and live trading modes
- 15-minute candle strategy with EMA, RSI, ADX and volume confirmation
- Real-time dashboard with equity, PnL, win rate and trade history
- Activity log with per-level filtering
