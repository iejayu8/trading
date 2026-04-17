/**
 * app.js – Multi-symbol Trading Bot Dashboard.
 *
 * Architecture:
 *  • Common data (equity, mode, total PnL/WR, logs) is fetched globally.
 *  • Live status cards (price, signal, setup, waiting, PnL, WR) are shown
 *    for ALL symbols simultaneously and refreshed every poll cycle.
 *  • Strategy parameters and market context (chart + conditions) are shown
 *    per the currently-selected tab symbol only.
 *  • Trade history and activity log are shared across all symbols.
 *  • Polls every 5 s via Promise.allSettled for resilience.
 */

// When running as a Home Assistant add-on, the ingress proxy path is injected
// by Flask as a <meta name="ingress-path"> tag. Use it so fetch() calls reach
// the correct URLs through the HA ingress reverse-proxy.
const _ingressPath = document.querySelector('meta[name="ingress-path"]')?.content ?? '';
const API = _ingressPath ? `${_ingressPath}/api` : '/api';
const POLL_INTERVAL = 5000;

let priceChart = null;
let _activeParamSymbol = null;  // selected symbol in Strategy Parameters
let _activeChartSymbol = null;  // selected symbol in Market Context
let _symbols = [];               // list from /api/symbols
let _copyTradingEnabled = false; // mirrors the DB copy trading toggle
let _copyTradingPendingApply = false; // true while user is entering a trader ID
let _tradingMode = 'realtrading'; // mirrors config.TRADING_MODE
let _botRunning = false;          // true when any symbol bot is running

// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await initSymbols();
  await refreshAll();
  setInterval(refreshAll, POLL_INTERVAL);
});

// Wire collapse buttons at script-evaluation time (app.js is bottom-of-body,
// so all elements exist in the DOM when this runs). This avoids any DOMContentLoaded
// async-timing issues in Home Assistant's ingress environment.
document.getElementById('btn-collapse-symbols')
  ?.addEventListener('click', () => togglePanel('all-symbols-status', 'btn-collapse-symbols'));
document.getElementById('btn-collapse-params')
  ?.addEventListener('click', () => togglePanel('param-body', 'btn-collapse-params'));
document.getElementById('btn-collapse-chart')
  ?.addEventListener('click', () => togglePanel('chart-body', 'btn-collapse-chart'));

// ── Symbol tab initialisation ─────────────────────────────────────────────

async function initSymbols() {
  try {
    _symbols = await fetchJSON(`${API}/symbols`);
  } catch {
    _symbols = ['BTC-USDT'];
  }
  _activeParamSymbol = _symbols[0];
  _activeChartSymbol = _symbols[0];
  renderParamTabs();
  renderChartTabs();
  renderAllSymbolsStatusGrid();
  await loadConfig(_activeParamSymbol);
}

/** Convert a symbol name to a safe DOM-id suffix (e.g. "BTC-USDT" → "btc_usdt"). */
function symId(sym) {
  return sym.replace(/[^a-zA-Z0-9]/g, '_').toLowerCase();
}

/** Build the all-symbols status grid from the loaded _symbols list. */
function renderAllSymbolsStatusGrid() {
  const container = document.getElementById('all-symbols-status');
  container.innerHTML = '';
  _symbols.forEach(sym => {
    const sid = symId(sym);
    const row = document.createElement('div');
    row.className = 'symbol-status-row';
    row.id = `sym-row-${sid}`;
    row.innerHTML = `
      <div class="symbol-row-label">${sym}</div>
      <div class="cards cards--symbol">
        <div class="card">
          <div class="card__label">Last Price</div>
          <div class="card__value" id="kpi-price-${sid}">–</div>
        </div>
        <div class="card">
          <div class="card__label">Last Signal</div>
          <div class="card__value" id="kpi-signal-${sid}">–</div>
        </div>
        <div class="card">
          <div class="card__label">Entry Setup</div>
          <div class="card__value text-gold" id="kpi-setup-${sid}">WAIT</div>
        </div>
        <div class="card">
          <div class="card__label">Waiting For</div>
          <div class="card__value card__value--small" id="kpi-waiting-${sid}">–</div>
        </div>
        <div class="card">
          <div class="card__label">Symbol PnL</div>
          <div class="card__value" id="kpi-sym-pnl-${sid}">–</div>
        </div>
        <div class="card">
          <div class="card__label">Symbol WR</div>
          <div class="card__value" id="kpi-sym-wr-${sid}">–</div>
        </div>
      </div>`;
    container.appendChild(row);
  });
}

function renderParamTabs() {
  const container = document.getElementById('param-tabs');
  container.innerHTML = '';
  _symbols.forEach(sym => {
    const btn = document.createElement('button');
    btn.className = 'panel-tab-btn' + (sym === _activeParamSymbol ? ' panel-tab-btn--active' : '');
    btn.textContent = sym;
    btn.dataset.symbol = sym;
    btn.onclick = () => switchParamSymbol(sym);
    container.appendChild(btn);
  });
}

function renderChartTabs() {
  const container = document.getElementById('chart-tabs');
  container.innerHTML = '';
  _symbols.forEach(sym => {
    const btn = document.createElement('button');
    btn.className = 'panel-tab-btn' + (sym === _activeChartSymbol ? ' panel-tab-btn--active' : '');
    btn.textContent = sym;
    btn.dataset.symbol = sym;
    btn.onclick = () => switchChartSymbol(sym);
    container.appendChild(btn);
  });
}

async function switchParamSymbol(sym) {
  if (sym === _activeParamSymbol) return;
  _activeParamSymbol = sym;
  document.querySelectorAll('#param-tabs .panel-tab-btn').forEach(b => {
    b.classList.toggle('panel-tab-btn--active', b.dataset.symbol === sym);
  });
  await loadConfig(sym);
}

async function switchChartSymbol(sym) {
  if (sym === _activeChartSymbol) return;
  _activeChartSymbol = sym;
  document.querySelectorAll('#chart-tabs .panel-tab-btn').forEach(b => {
    b.classList.toggle('panel-tab-btn--active', b.dataset.symbol === sym);
  });
  await refreshSymbolChart(sym);
}

function togglePanel(bodyId, btnId) {
  const body = document.getElementById(bodyId);
  const btn  = document.getElementById(btnId);
  const collapsed = body.classList.toggle('collapsed');
  btn.textContent = collapsed ? '▼' : '▲';
  btn.title = collapsed ? 'Expand' : 'Collapse';
  // Resize chart canvas after expanding so Chart.js recalculates dimensions
  if (!collapsed && priceChart) {
    setTimeout(() => priceChart.resize(), 50);
  }
}

// ── Fetch helpers ──────────────────────────────────────────────────────────

/** Counter of in-flight API requests. Controls the exchange LED. */
let _pendingRequests = 0;

function _updateExchangeLed() {
  const el = document.getElementById('exchange-indicator');
  if (!el) return;
  if (_pendingRequests > 0) {
    el.className = 'indicator indicator--active';
    el.title = `Exchange requests: ${_pendingRequests} in flight`;
  } else {
    el.className = 'indicator indicator--idle';
    el.title = 'Exchange requests';
  }
}

/** Drop-in replacement for fetch() that lights the exchange LED while active. */
async function apiFetch(url, opts) {
  _pendingRequests++;
  _updateExchangeLed();
  try {
    return await fetch(url, opts);
  } finally {
    _pendingRequests--;
    _updateExchangeLed();
  }
}

async function fetchJSON(url) {
  const resp = await apiFetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${url}`);
  return resp.json();
}

// ── Main refresh ──────────────────────────────────────────────────────────

async function refreshAll() {
  await Promise.allSettled([
    refreshGlobal(),
    refreshAllSymbolCards(),
    refreshSymbolChart(_activeChartSymbol),
    refreshCommonTradePanels(),
    refreshLogs(),
    loadTradingMode(),
    loadCopyTradingConfig(),
    refreshCopyPositions(),
  ]);
}

// ── Global (common) section ───────────────────────────────────────────────

async function refreshGlobal() {
  await Promise.allSettled([
    refreshBotStatus(),
    refreshStats(),
  ]);
}

async function refreshBotStatus() {
  let allStatus;
  try {
    allStatus = await fetchJSON(`${API}/status`);
  } catch { return; }

  // Bot running = any symbol is running
  const anyRunning = Object.values(allStatus).some(s => s.running === 1);
  _botRunning = anyRunning;
  document.getElementById('bot-indicator').className =
    `indicator ${anyRunning ? 'indicator--running' : 'indicator--stopped'}`;
  document.getElementById('bot-status-text').textContent = anyRunning ? 'Running' : 'Stopped';
  document.getElementById('btn-start').disabled = anyRunning;
  document.getElementById('btn-stop').disabled  = !anyRunning;

  updateCopyTradingPanelVisibility();

  // Mode + equity from active symbol's status
  const activeStatus = allStatus[_activeParamSymbol] || {};
  const modeEl = document.getElementById('kpi-mode');
  const rawMode = String(activeStatus.trading_mode || 'realtrading').toLowerCase();
  const displayMode = _copyTradingEnabled ? 'copytrading' : rawMode;
  modeEl.textContent = displayMode.replace('copytrading', 'COPY TRADING').toUpperCase();
  modeEl.className = 'card__value ' + modeClass(displayMode);

  let equity = activeStatus.equity != null ? activeStatus.equity : null;
  if (equity === null) {
    for (const s of Object.values(allStatus)) {
      if (s.equity != null) { equity = s.equity; break; }
    }
  }
  document.getElementById('kpi-equity').textContent =
    equity != null ? `$${Number(equity).toFixed(2)}` : '–';

  // Update signal cards from bulk status (DB). Price is intentionally skipped
  // here because refreshOneSymbolCards() fetches a live price from the exchange
  // and updating from the DB (which is only written every 15 min by the bot
  // tick) would race against and overwrite the fresher live value.
  for (const [sym, s] of Object.entries(allStatus)) {
    const sid = symId(sym);
    const sigEl = document.getElementById(`kpi-signal-${sid}`);
    if (sigEl) {
      const sig = (s.last_signal && s.last_signal !== 'NONE') ? s.last_signal : '–';
      sigEl.textContent = sig;
      sigEl.className = 'card__value ' + signalClass(s.last_signal);
    }
  }
}

async function refreshStats() {
  let st;
  try { st = await fetchJSON(`${API}/stats`); } catch { return; }

  const pnlEl = document.getElementById('kpi-pnl');
  const pnl = Number(st.total_pnl || 0);
  pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
  pnlEl.className = 'card__value ' + (pnl >= 0 ? 'text-green' : 'text-red');

  document.getElementById('kpi-winrate').textContent = st.total ? `${st.win_rate}%` : '–';
  document.getElementById('kpi-total-trades').textContent = st.total || 0;
}

// ── All-symbols status cards ──────────────────────────────────────────────

/** Refresh PnL/WR and live price/setup/waiting for every symbol simultaneously. */
async function refreshAllSymbolCards() {
  await Promise.allSettled(_symbols.map(sym => refreshOneSymbolCards(sym)));
}

async function refreshOneSymbolCards(sym) {
  const sid = symId(sym);
  await Promise.allSettled([
    // Stats → PnL, Win Rate
    (async () => {
      try {
        const st = await fetchJSON(`${API}/stats?symbol=${encodeURIComponent(sym)}`);
        const pnlEl = document.getElementById(`kpi-sym-pnl-${sid}`);
        const wrEl  = document.getElementById(`kpi-sym-wr-${sid}`);
        if (!pnlEl || !wrEl) return;
        const pnl = Number(st.total_pnl || 0);
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
        pnlEl.className = 'card__value ' + (pnl >= 0 ? 'text-green' : 'text-red');
        wrEl.textContent = st.total ? `${st.win_rate}%` : '–';
      } catch {}
    })(),
    // Market context (lightweight) → live price, setup, waiting
    (async () => {
      try {
        const ctx = await fetchJSON(`${API}/market/context?symbol=${encodeURIComponent(sym)}&limit=50`);
        if (!ctx.ok) return;
        if (ctx.values && ctx.values.close != null) {
          const priceEl = document.getElementById(`kpi-price-${sid}`);
          if (priceEl) priceEl.textContent = formatPrice(ctx.values.close);
        }
        if (ctx.diagnostics) {
          const hint = ctx.diagnostics.signal_hint || 'WAIT';
          const setupEl = document.getElementById(`kpi-setup-${sid}`);
          if (setupEl) {
            setupEl.textContent = hint.replaceAll('_', ' ');
            setupEl.className = 'card__value ' + setupClass(hint);
          }
          const waitEl = document.getElementById(`kpi-waiting-${sid}`);
          if (waitEl) waitEl.textContent = ctx.diagnostics.waiting_for || 'Collecting candles';
        }
      } catch {}
    })(),
  ]);
}

// ── Per-symbol tab panel (chart + conditions) ─────────────────────────────

async function loadConfig(sym) {
  let cfg;
  try { cfg = await fetchJSON(`${API}/config?symbol=${encodeURIComponent(sym)}`); }
  catch { return; }
  if (sym !== _activeParamSymbol) return;

  const tbody= document.querySelector('#param-table tbody');
  tbody.innerHTML = '';
  const mode = String(cfg.trading_mode || 'realtrading').toLowerCase();
  const rows = [
    ['Symbol',           cfg.symbol],
    ['Trading Mode',     mode],
    ['Timeframe',        cfg.timeframe],
    ['Leverage',         `${cfg.leverage}×`],
    ['Risk / Trade',     `${Number(cfg.risk_per_trade_pct).toFixed(2)}%`],
    ['Stop Loss',        `${Number(cfg.stop_loss_pct).toFixed(2)}%`],
    ['Take Profit',      `${Number(cfg.take_profit_pct).toFixed(2)}%`],
    ['Fast EMA',         cfg.fast_ema],
    ['Slow EMA',         cfg.slow_ema],
    ['Trend EMA',        cfg.trend_ema],
    ['RSI Period',       cfg.rsi_period],
    ['RSI Oversold',     cfg.rsi_oversold],
    ['RSI Overbought',   cfg.rsi_overbought],
    ['Volume SMA',       cfg.volume_sma_period],
  ];
  rows.forEach(([k, v]) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${k}</td><td><strong>${v}</strong></td>`;
    tbody.appendChild(tr);
  });
}

async function refreshSymbolChart(sym) {
  let ctx;
  try { ctx = await fetchJSON(`${API}/market/context?symbol=${encodeURIComponent(sym)}&limit=200`); }
  catch { return; }
  if (sym !== _activeChartSymbol) return;
  if (!ctx.ok || !ctx.candles || !ctx.candles.length) return;

  const labels = ctx.candles.map(c => fmtTs(c.ts));
  const close = ctx.candles.map(c => c.close);
  const emaFast = ctx.candles.map(c => c.ema_fast);
  const emaSlow = ctx.candles.map(c => c.ema_slow);
  const emaTrend = ctx.candles.map(c => c.ema_trend);
  const ema200 = ctx.candles.map(c => c.ema_200);

  const longTarget  = ctx.target_band?.long  || null;
  const shortTarget = ctx.target_band?.short || null;
  const longUpper  = longTarget  ? new Array(close.length).fill(longTarget.high)  : null;
  const longLower  = longTarget  ? new Array(close.length).fill(longTarget.low)   : null;
  const shortUpper = shortTarget ? new Array(close.length).fill(shortTarget.high) : null;
  const shortLower = shortTarget ? new Array(close.length).fill(shortTarget.low)  : null;

  const datasets = [
    { label: 'Price',  data: close,    borderColor: '#f0b90b', borderWidth: 2,   pointRadius: 0, tension: 0.2 },
    { label: 'EMA9',   data: emaFast,  borderColor: '#3fb950', borderWidth: 1.6, pointRadius: 0, tension: 0.2 },
    { label: 'EMA21',  data: emaSlow,  borderColor: '#388bfd', borderWidth: 1.4, pointRadius: 0, tension: 0.2 },
    { label: 'EMA55',  data: emaTrend, borderColor: '#f97316', borderWidth: 1.2, pointRadius: 0, tension: 0.2 },
    { label: 'EMA200', data: ema200,   borderColor: '#8b949e', borderWidth: 1,   pointRadius: 0, tension: 0.2 },
  ];

  if (longUpper && longLower) {
    datasets.push(
      { label: 'Long Zone Upper', data: longUpper, borderColor: 'rgba(63,185,80,0.4)',  backgroundColor: 'rgba(63,185,80,0.12)',  borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
      { label: 'Long Target Zone', data: longLower, borderColor: 'rgba(63,185,80,0.4)', backgroundColor: 'rgba(63,185,80,0.12)',  borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: '-1' }
    );
  }
  if (shortUpper && shortLower) {
    datasets.push(
      { label: 'Short Zone Upper', data: shortUpper, borderColor: 'rgba(248,81,73,0.45)', backgroundColor: 'rgba(248,81,73,0.12)', borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
      { label: 'Short Target Zone', data: shortLower, borderColor: 'rgba(248,81,73,0.45)', backgroundColor: 'rgba(248,81,73,0.12)', borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: '-1' }
    );
  }

  const chartData = { labels, datasets };
  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        labels: {
          color: '#e6edf3',
          filter: item => !['Long Zone Upper','Short Zone Upper'].includes(item.text),
        },
      },
    },
    scales: {
      x: { ticks: { color: '#8b949e', maxRotation: 0 }, grid: { color: '#1f2937' } },
      y: { ticks: { color: '#8b949e' }, grid: { color: '#1f2937' } },
    },
  };

  const canvas = document.getElementById('price-chart');
  if (!priceChart) {
    priceChart = new Chart(canvas, { type: 'line', data: chartData, options: chartOptions });
  } else {
    priceChart.data = chartData;
    priceChart.update('none');
  }

  renderChecks('long-checks',  ctx.long_checks  || {});
  renderChecks('short-checks', ctx.short_checks || {});
}

// ── Trade history & logs (common) ─────────────────────────────────────────

async function refreshCommonTradePanels() {
  await refreshTradeHistory();
}

async function refreshTradeHistory() {
  let trades, statusMap;
  try { trades = await fetchJSON(`${API}/trades?limit=50`); }
  catch { return; }

  // Fetch current prices per symbol for unrealised PnL on open trades.
  try {
    const allStatus = await fetchJSON(`${API}/status`);
    statusMap = allStatus;
  } catch {
    statusMap = {};
  }

  const tbody = document.querySelector('#trade-table tbody');
  tbody.innerHTML = '';
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="16" class="empty-msg" style="padding:12px">No trades yet</td></tr>';
    return;
  }

  trades.forEach(t => {
    const pnl = t.pnl != null ? Number(t.pnl) : null;
    const pnlStr = pnl != null
      ? `<span class="${pnl >= 0 ? 'text-green' : 'text-red'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>`
      : '–';
    const value = Number(t.size) * Number(t.entry_price);

    const entry  = Number(t.entry_price);
    const size   = Number(t.size);
    const isLong = t.direction === 'LONG';

    let slLossStr = '–';
    if (t.sl_price != null) {
      const sl = Number(t.sl_price);
      const slLoss = isLong ? (sl - entry) * size : (entry - sl) * size;
      slLossStr = `<span class="text-red">${slLoss.toFixed(2)}</span>`;
    }

    let tpProfitStr = '–';
    if (t.tp_price != null) {
      const tp = Number(t.tp_price);
      const tpProfit = isLong ? (tp - entry) * size : (entry - tp) * size;
      tpProfitStr = `<span class="text-green">+${tpProfit.toFixed(2)}</span>`;
    }

    // Current Profit: unrealised for OPEN trades, realised for CLOSED trades.
    let currentProfitStr = '–';
    if (t.status === 'OPEN') {
      const symStatus = statusMap[t.symbol];
      const cp = symStatus && symStatus.last_price != null ? Number(symStatus.last_price) : null;
      if (cp != null) {
        const cpPnl = isLong ? (cp - entry) * size : (entry - cp) * size;
        currentProfitStr = `<span class="${cpPnl >= 0 ? 'text-green' : 'text-red'}">${cpPnl >= 0 ? '+' : ''}${cpPnl.toFixed(2)}</span>`;
      }
    } else if (t.exit_price != null) {
      const exitP = Number(t.exit_price);
      const cpPnl = isLong ? (exitP - entry) * size : (entry - exitP) * size;
      currentProfitStr = `<span class="${cpPnl >= 0 ? 'text-green' : 'text-red'}">${cpPnl >= 0 ? '+' : ''}${cpPnl.toFixed(2)}</span>`;
    }

    const actionCell = t.status === 'OPEN'
      ? `<button class="btn btn--danger btn--small" onclick="closeTrade(${t.id}, '${t.symbol}')">Close</button>`
      : '–';

    tbody.innerHTML += `
      <tr>
        <td>${t.id}</td>
        <td><strong>${t.symbol}</strong></td>
        <td><strong>${t.direction}</strong></td>
        <td>${formatPrice(t.entry_price)}</td>
        <td>${t.exit_price ? formatPrice(t.exit_price) : '–'}</td>
        <td>${t.size}</td>
        <td>$${value.toFixed(2)}</td>
        <td>${formatPrice(t.sl_price)}</td>
        <td>${formatPrice(t.tp_price)}</td>
        <td>${slLossStr}</td>
        <td>${tpProfitStr}</td>
        <td>${currentProfitStr}</td>
        <td>${pnlStr}</td>
        <td>${statusBadge(t.status)}</td>
        <td>${fmtTs(t.opened_at)}</td>
        <td>${actionCell}</td>
      </tr>`;
  });
}

async function closeTrade(tradeId, symbol) {
  if (!confirm(`Close trade #${tradeId} (${symbol}) at current market price?`)) return;
  try {
    const res = await apiFetch(`${API}/trades/${tradeId}/close`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      await refreshTradeHistory();
    } else {
      alert(`Failed to close trade: ${data.message}`);
    }
  } catch (e) {
    alert(`Error closing trade: ${e.message}`);
  }
}

// ── Activity log (common) ─────────────────────────────────────────────────

async function refreshLogs() {
  let logs;
  try {
    logs = await fetchJSON(`${API}/logs?limit=60`);
  } catch (e) {
    console.error('Activity log fetch failed:', e);
    const container = document.getElementById('log-container');
    if (container && !container.innerHTML.trim()) {
      container.innerHTML = '<div class="log-empty">Unable to load activity log — check backend connection.</div>';
    }
    return;
  }

  const container = document.getElementById('log-container');

  if (!logs || logs.length === 0) {
    container.innerHTML = '<div class="log-empty">No activity yet — start the bot to begin logging.</div>';
    return;
  }

  const html = [...logs].reverse().map(l => `
    <div class="log-entry">
      <span class="log-ts">${fmtTs(l.ts)}</span>
      <span class="log-level-${l.level}">[${l.level}]</span>
      <span>${escHtml(l.message)}</span>
    </div>`).join('');

  container.innerHTML = html;
  container.scrollTop = container.scrollHeight;
}

// ── Bot control ────────────────────────────────────────────────────────────

async function startBot() {
  try {
    const r = await apiFetch(`${API}/bot/start`, { method: 'POST' });
    const data = await r.json();
    if (!data.ok) alert(data.message);
    refreshAll();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

async function stopBot() {
  try {
    const r = await apiFetch(`${API}/bot/stop`, { method: 'POST' });
    const data = await r.json();
    if (!data.ok) alert(data.message);
    refreshAll();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

async function clearLogs() {
  if (!confirm('Clear all activity log entries?')) return;
  try {
    const r = await apiFetch(`${API}/logs/clear`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) { alert(data.message || 'Could not clear log'); return; }
    await refreshLogs();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

async function resetDatabase() {
  if (!confirm('Reset all statistics?\n\nThis will permanently delete all trade history, activity logs, and bot status data. This action cannot be undone.\n\nMake sure all bots are stopped before resetting.')) return;
  try {
    const r = await apiFetch(`${API}/database/reset`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) { alert(data.message || 'Could not reset database'); return; }
    await refreshAll();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

// ── Trading mode (paper / real) controls ────────────────────────────────────

async function loadTradingMode() {
  let data;
  try { data = await fetchJSON(`${API}/trading/mode`); } catch { return; }

  _tradingMode = data.mode || 'realtrading';
  const btnPaper = document.getElementById('mode-btn-paper');
  const btnReal  = document.getElementById('mode-btn-real');

  if (btnPaper && btnReal) {
    btnPaper.classList.toggle('mode-btn--active', _tradingMode === 'papertrading');
    btnPaper.classList.toggle('mode-btn--paper', _tradingMode === 'papertrading');
    btnReal.classList.toggle('mode-btn--active', _tradingMode === 'realtrading');
  }
}

async function switchTradingMode(mode) {
  if (mode === _tradingMode) return;

  const label = mode === 'papertrading' ? 'Paper Trading' : 'Real Trading';
  if (!confirm(`Switch to ${label}?\n\nAll bots will be stopped. You will need to restart them manually.`)) return;

  // Stop bots first
  try {
    await apiFetch(`${API}/bot/stop`, { method: 'POST' });
  } catch { /* bots may already be stopped */ }

  try {
    const r = await apiFetch(`${API}/trading/mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    const data = await r.json();
    if (!data.ok) { alert(`Error switching mode: ${data.message}`); return; }
  } catch (e) { alert(`Could not switch trading mode: ${e.message}`); return; }

  await refreshAll();
}

// ── Mode selector & copy trading controls ──────────────────────────────────

function updateCopyTradingUI(enabled, trader_id) {
  const inputWrap = document.getElementById('copy-trader-input-wrap');
  const idInput = document.getElementById('copy-trader-id');
  const statusEl = document.getElementById('copy-trading-status');
  const btnStrategy = document.getElementById('mode-btn-strategy');
  const btnCopy = document.getElementById('mode-btn-copy');

  // Sync toggle buttons
  if (btnStrategy && btnCopy) {
    btnStrategy.classList.toggle('mode-btn--active', !enabled);
    btnCopy.classList.toggle('mode-btn--active', enabled);
    btnCopy.classList.toggle('mode-btn--copy', enabled);
  }

  if (inputWrap) inputWrap.classList.toggle('hidden', !enabled);
  if (idInput && trader_id) idInput.value = trader_id;
  if (statusEl) {
    if (enabled && trader_id) {
      statusEl.textContent = `Mirroring: ${trader_id}`;
      statusEl.className = 'copy-trading-status text-purple';
    } else if (enabled) {
      statusEl.textContent = 'Copy trading on \u2013 enter a trader ID';
      statusEl.className = 'copy-trading-status text-gold';
    } else {
      statusEl.textContent = 'Custom strategy active';
      statusEl.className = 'copy-trading-status';
    }
  }

  updateCopyTradingPanelVisibility();
}

async function loadCopyTradingConfig() {
  // Don't override local UI while the user is entering a trader ID
  if (_copyTradingPendingApply) return;

  let cfg;
  try { cfg = await fetchJSON(`${API}/copytrading/config`); } catch { return; }

  _copyTradingEnabled = !!cfg.enabled;
  updateCopyTradingUI(_copyTradingEnabled, cfg.trader_id);
}

async function switchMode(mode) {
  if (mode === 'strategy' && !_copyTradingEnabled) return;
  if (mode === 'copy' && _copyTradingEnabled) return;

  if (mode === 'strategy') {
    // Disable copy trading immediately
    _copyTradingPendingApply = false;
    try {
      const r = await apiFetch(`${API}/copytrading/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: false, trader_id: '' }),
      });
      const data = await r.json();
      if (!data.ok) { alert(`Error switching mode: ${data.message}`); return; }
    } catch (e) { alert(`Could not save mode: ${e.message}`); return; }
    await refreshAll();
    return;
  }

  // mode === 'copy': show the copy trading UI without saving to the backend yet.
  // (The backend requires a non-empty trader_id before it will accept enabled=true,
  // so we update the UI locally and wait for the user to enter an ID and click Apply.)
  _copyTradingEnabled = true;
  _copyTradingPendingApply = true;
  const idInput = document.getElementById('copy-trader-id');
  updateCopyTradingUI(true, idInput ? idInput.value.trim() : '');
}

async function saveCopyTradingConfig() {
  const idInput = document.getElementById('copy-trader-id');
  const trader_id = idInput ? idInput.value.trim() : '';

  try {
    const r = await apiFetch(`${API}/copytrading/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: true, trader_id }),
    });
    const data = await r.json();
    if (!data.ok) { _copyTradingPendingApply = false; alert(`Copy trading config error: ${data.message}`); return; }
    await refreshAll();
    _copyTradingPendingApply = false;
  } catch (e) { _copyTradingPendingApply = false; alert(`Could not save copy trading config: ${e.message}`); }
}

// ── Copy Trading panel visibility & open positions ──────────────────────────

/**
 * Show the copy-positions panel and hide strategy sections when copy trading
 * is active AND the bot is running. Restore the normal view otherwise.
 */
function updateCopyTradingPanelVisibility() {
  const showCopy = _copyTradingEnabled && _botRunning;

  const symbolStatusPanel = document.getElementById('all-symbols-status-panel');
  const symbolPanel       = document.getElementById('symbol-panel');
  const copyPanel         = document.getElementById('copy-positions-panel');

  if (symbolStatusPanel) symbolStatusPanel.classList.toggle('hidden', showCopy);
  if (symbolPanel)       symbolPanel.classList.toggle('hidden', showCopy);
  if (copyPanel)         copyPanel.classList.toggle('hidden', !showCopy);
}

/** Fetch open trades from the backend and render them in the copy positions table. */
async function refreshCopyPositions() {
  if (!_copyTradingEnabled || !_botRunning) return;

  let trades, statusMap;
  try { trades = await fetchJSON(`${API}/trades/open`); } catch { return; }
  try { statusMap = await fetchJSON(`${API}/status`); } catch { statusMap = {}; }

  const tbody = document.querySelector('#copy-positions-table tbody');
  if (!tbody) return;

  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-msg" style="padding:12px">No open positions</td></tr>';
    return;
  }

  tbody.innerHTML = '';
  trades.forEach(t => {
    const entry  = Number(t.entry_price);
    const size   = Number(t.size);
    const isLong = t.direction === 'LONG';
    const value  = size * entry;

    let currentProfitStr = '–';
    const symStatus = statusMap[t.symbol];
    const cp = symStatus && symStatus.last_price != null ? Number(symStatus.last_price) : null;
    if (cp != null) {
      const cpPnl = isLong ? (cp - entry) * size : (entry - cp) * size;
      currentProfitStr = `<span class="${cpPnl >= 0 ? 'text-green' : 'text-red'}">${cpPnl >= 0 ? '+' : ''}${cpPnl.toFixed(2)}</span>`;
    }

    tbody.innerHTML += `
      <tr>
        <td>${t.id}</td>
        <td><strong>${escHtml(t.symbol)}</strong></td>
        <td><strong>${escHtml(t.direction)}</strong></td>
        <td>${formatPrice(t.entry_price)}</td>
        <td>${t.size}</td>
        <td>$${value.toFixed(2)}</td>
        <td>${formatPrice(t.sl_price)}</td>
        <td>${formatPrice(t.tp_price)}</td>
        <td>${currentProfitStr}</td>
        <td>${fmtTs(t.opened_at)}</td>
      </tr>`;
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────

function formatPrice(value) {
  return `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function signalClass(sig) {
  if (sig === 'LONG')  return 'text-green';
  if (sig === 'SHORT') return 'text-red';
  return '';
}

function setupClass(hint) {
  if (hint === 'LONG_READY')  return 'text-green';
  if (hint === 'SHORT_READY') return 'text-red';
  if (hint === 'COOLDOWN')    return 'text-blue';
  return 'text-gold';
}

function modeClass(mode) {
  if (mode === 'papertrading') return 'text-blue';
  if (mode === 'realtrading')  return 'text-green';
  if (mode === 'copytrading')  return 'text-purple';
  return 'text-gold';
}

function renderChecks(containerId, checks) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const items = Object.entries(checks);

  if (!items.length) {
    // Only rewrite if it's not already showing the "No data" placeholder
    const existing = el.querySelector('li');
    if (!existing || existing.dataset.checkKey !== '__nodata__') {
      el.innerHTML = '<li data-check-key="__nodata__"><span>No data</span><span class="check-badge check-badge--no">N/A</span></li>';
    }
    return;
  }

  const existingItems = el.querySelectorAll('li[data-check-key]');
  const existingKeys  = Array.from(existingItems).map(li => li.dataset.checkKey);
  const newKeys       = items.map(([name]) => name);

  // If the set of condition keys changed, do a full rebuild (rare – only on symbol switch)
  if (existingKeys.join('|') !== newKeys.join('|')) {
    el.innerHTML = items.map(([name, ok]) => {
      const cls  = ok ? 'check-badge--ok' : 'check-badge--no';
      const text = ok ? 'OK' : 'WAIT';
      return `<li data-check-key="${escHtml(name)}"><span>${escHtml(name)}</span><span class="check-badge ${cls}">${text}</span></li>`;
    }).join('');
    return;
  }

  // Keys match — patch only badges that changed
  existingItems.forEach((li, i) => {
    const [, ok] = items[i];
    const badge = li.querySelector('.check-badge');
    if (!badge) return;
    const wantCls  = ok ? 'check-badge--ok' : 'check-badge--no';
    const wantText = ok ? 'OK' : 'WAIT';
    if (badge.textContent !== wantText) badge.textContent = wantText;
    if (!badge.classList.contains(wantCls)) {
      badge.classList.remove('check-badge--ok', 'check-badge--no');
      badge.classList.add(wantCls);
    }
  });
}

function statusBadge(status) {
  const cls = status === 'OPEN' ? 'text-blue' : (status === 'CLOSED' ? 'text-muted' : '');
  return `<span class="${cls}">${status}</span>`;
}

function fmtTs(ts) {
  if (!ts) return '–';
  let normalized = String(ts).trim().replace(' ', 'T');
  const hasTz = /(?:Z|[+-]\d{2}:\d{2})$/i.test(normalized);
  if (!hasTz) normalized += 'Z';
  const d = new Date(normalized);
  if (isNaN(d)) return ts;
  return d.toLocaleString('en-US', {
    month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  });
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

