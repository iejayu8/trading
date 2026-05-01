# Changelog

## 1.7.31
- Merge pull request #48 from iejayu8/copilot/fix-no-candle-data-error

## 1.7.30
- fix: seed `before_ts` to current time in `get_candles` so `history-candles` always receives a `before` parameter (endpoint silently returns `[]` without it)
- fix: escape `X-Ingress-Path` header with `html.escape()` before injecting into HTML to prevent XSS
- fix: hold `_bots_lock` around `_bots.clear()` in mode-switch and copy-trading toggle endpoints (race with concurrent `_get_bot()`)
- fix: add `denominator <= 0` guard in `calculate_position_size` to prevent division-by-zero when `stop_loss_pct=0`; extract `MIN_POSITION_SIZE = 0.001` constant
- fix: remove dead `leverage` parameter from `calculate_position_size` (leverage does not affect sizing)
- fix: portfolio margin cap now uses each trade's own recorded `leverage` from DB instead of `self.leverage` for cross-symbol accuracy
- fix: breakeven trades (`pnl = 0`) no longer counted as losses in `get_trade_stats` (`pnl <= 0` → `pnl < 0`)
- fix: close `BloFinClient._session` after each `/api/market/context` call to prevent file-descriptor leaks
- fix: use `timeout=(5, 10)` on all HTTP calls so TCP connect phase is also bounded
- fix: replace `get_trade_history(limit=200)` + Python date-filter in daily loss guard with new `get_daily_pnl(date_iso)` date-scoped SQL query — no longer misses trades on high-frequency days
- backtest: to reproduce last-month 15m backtest run `python backtest/backtest.py --all --fresh --days 30 --equity 1000`

- Merge pull request #47 from iejayu8/copilot/fix-papertrading-position-issues

## 1.7.28
- chore: bump version to 1.7.28

## 1.7.27
- fix: **positions never opened in paper trading** — two confirmed root causes resolved:
  1. **BloFin API hard-cap of 100 candles per request**: the bot was requesting `limit=200` but receiving only 100 candles, so `len(df)=100 < MIN_BARS_REQUIRED=200` permanently blocked all signal generation (`generate_signal` always returned `NONE`). Fixed by switching `get_candles` to the `history-candles` endpoint with cursor-based pagination (same pattern already used in `backtest/fetch_data.py` where the 100-candle limit was explicitly documented).
  2. **RSI pullback thresholds too low for bull-market conditions**: in a strong uptrend BTC/ETH RSI oscillates in the 62–80 range; the v8 pullback threshold of ≤ 60 was never reached within the 6-bar window, so "Recent RSI pullback" never passed and all LONG signals were blocked. Per-symbol thresholds raised: BTC/ETH `rsi_pullback_max` 60 → 65, `rsi_recovery_long` 63 → 68; SOL/XRP/LINK 50 → 55 / 53 → 58. Module-level defaults raised from 52/55 to 55/58. Explicit SHORT thresholds (`rsi_pullback_min`, `rsi_recovery_short`) added to BTC/ETH params to preserve tested SHORT behaviour (mirrors of the v8 values) so raising LONG params does not accidentally loosen SHORT entries.
- refactor: `get_signal_checks` now resolves `rsi_pullback_min` and `rsi_recovery_short` from `sym_params` first (explicit override), then falls back to the symmetric mirror formula — enables independent LONG/SHORT RSI tuning per symbol without changing the strategy logic for symbols that don't need it.


- fix: document live optimizer baseline
- fix: polish changelog and optimizer naming
- fix: address validation feedback

## 1.7.25
- fix: preserve the last known copy-trading state in `start()`, `_tick()`, `_tick_copy_only()`, and `_run_loop()` when reading DB config fails — transient DB errors no longer disable mirroring or bypass the intended entry path
- fix: align frontend/default trading-mode fallbacks with `papertrading` so the dashboard no longer boots showing the stale real-trading state that previously blocked entries without credentials
- fix: align `backtest/optimize.py` with the live strategy by using ATR-normalised MACD gates, shared volume thresholds, and v8-era search ranges/baselines

## 1.7.23
- fix: update `test_trading_mode_invalid_resets_leverage` to expect `"papertrading"` fallback (was `"realtrading"`)
- fix: copy trading fallback in `_run_loop()` now uses last known state instead of `{}` — prevents inadvertent mode switch to custom strategy on transient DB errors
- fix: update `make_short_trigger_ohlcv` test fixture with percentage-based moves — old fixture produced RSI values below the 48 threshold (RSI_PULLBACK_MIN), causing SHORT signal test to fail
- fix: backtest `trade["pnl"]` now includes opening fee so `sum(trade["pnl"]) ≈ final_equity − initial_equity` (equity conservation)

## 1.7.22
- fix: resolve bugs preventing position opening + bump version to 1.7.21
- Initial plan

## 1.7.21
- fix: change default TRADING_MODE from "realtrading" to "papertrading" — bots in realtrading without API credentials had equity=None, blocking all position entries
- fix: protect _run_loop() db.get_copy_trading_config() call with try/except — an unhandled DB error silently killed the trading loop
- fix: add BTC-USDT and ETH-USDT per-symbol RSI overrides (pullback_max=60, recovery_long=63) — v8 threshold of 52 prevented LONG signals in strong bull markets (RSI 60–75) where RSI never dips that low
- fix: invalid TRADING_MODE fallback now defaults to "papertrading" instead of "realtrading"

## 1.7.20
- Merge pull request #42 from iejayu8/copilot/custom-strategy-bug-fix

## 1.7.19
- fix: relax custom strategy parameters to eliminate zero-signal regime (v8)
- Strategy v8: ADX_MIN 20→16, RSI_PULLBACK_MAX 46→52, RSI_RECOVERY_LONG 49→55, PULLBACK_LOOKBACK 3→6 bars (90 min)
- Per-symbol v8: SOL/XRP ADX 22→18, pullback 44/42→50, recovery 52→53, lookback 5→8; LINK ADX 18→14, pullback 42→50, recovery 52→53, lookback 5→8
- Root cause: v7 conditions (deep RSI dip within 45 min window) almost never aligned in post-ATH ranging markets, producing zero signals across all 5 symbols over 2+ days

## 1.7.18
- Replace Binance API with BloFin API for backtest data fetching
- Address review comments: rename variable, add docstring params
- Align backtest with bot: fix position sizing, fee double-count, daily loss guard, add --all flag

## 1.7.17
- Merge pull request #40 from iejayu8/copilot/analyze-app-for-issues

## 1.7.16
- fix: portfolio-wide paper equity and daily loss guard (was per-symbol, allowing 5× intended drawdown)
- fix: daily loss guard uses PAPER_START_EQUITY as stable denominator in paper mode
- fix: raise MAX_PORTFOLIO_RISK_PCT from 3% to 5% to prevent boundary blocking from position size rounding
- fix: relax per-symbol strategy params for SOL, XRP, LINK (signal generation was near-impossible)

## 1.7.15
- feat: 5-second copy trading sync interval with lightweight _tick_copy_only

## 1.7.14
- docs: clarify first_equity return value in docstring
- fix: update equity from exchange when realtrading mode is activated

## 1.7.13
- fix: update equity (USDT) from exchange as soon as realtrading mode is activated
- Equity is now refreshed even when re-selecting the already-active mode
- Mode switch API response includes the equity value for immediate frontend display

## 1.7.12
- test: add 22 tests for mode-specific DB isolation; sync trading-bot/
- feat: mode-specific database separation for 4 operating modes

## 1.7.11
- Fix Live Symbol Status collapse with !important CSS and inline style fallback

## 1.7.10
- fix: clear _copyTradingPending on save error to prevent infinite poll block
- fix: prevent poll from hiding copy trading input before user applies

## 1.7.9
- Fix XSS: escape t.id and t.size, use array accumulator for innerHTML
- Disable strategy sections and show open positions when copy trading is active and bot is running

## 1.7.8
- Hide Live Symbol Status, Strategy Parameters & Market Context panels when copy trading is active and bot is running; show a Copy Trading – Open Positions table with live unrealised PnL instead

## 1.7.7
- Fix all 4 header mode buttons: proper backend integration, disable when bots running, fix copy trading toggle

## 1.7.6
- Fix Live Symbol Status collapse button CSS specificity bug

## 1.7.5
- Address code review feedback: fix import alias, test isolation, and INSERT OR IGNORE
- Fix import bugs in app.py and add comprehensive tests for 100% coverage
- Initial analysis - identify code issues and coverage gaps

## 1.7.4
- Address review: clear _copyTradingPendingApply on error and after refreshAll
- Fix copy trading dialog hidden by polling and equity not updating on mode switch

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
