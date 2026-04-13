# Changelog

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
