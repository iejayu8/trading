/**
 * app.js – Multi-symbol Trading Bot Dashboard.
 *
 * Architecture:
 *  • Common data (equity, mode, total PnL/WR, logs) is fetched globally.
 *  • Per-symbol data (price, signal, setup, chart) is
 *    fetched for the currently-selected tab symbol.
 *  • Trade history is shared across all symbols.
 *  • Activity log is shared across all symbols (bottom of page).
 *  • Polls every 5 s via Promise.allSettled for resilience.
 */

const API = '/api';
const POLL_INTERVAL = 5000;

let priceChart = null;
let _activeSymbol = null;   // currently selected tab
let _symbols = [];           // list from /api/symbols

// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await initSymbols();
  await refreshAll();
  setInterval(refreshAll, POLL_INTERVAL);
});

// ── Symbol tab initialisation ─────────────────────────────────────────────

async function initSymbols() {
  try {
    _symbols = await fetchJSON(`${API}/symbols`);
  } catch {
    _symbols = ['BTC-USDT'];
  }
  _activeSymbol = _symbols[0];
  renderTabs();
  await loadConfig(_activeSymbol);
}

function renderTabs() {
  const container = document.getElementById('symbol-tabs');
  container.innerHTML = '';
  _symbols.forEach(sym => {
    const btn = document.createElement('button');
    btn.className = 'tab-btn' + (sym === _activeSymbol ? ' tab-btn--active' : '');
    btn.textContent = sym;
    btn.dataset.symbol = sym;
    btn.onclick = () => switchSymbol(sym);
    container.appendChild(btn);
  });
}

async function switchSymbol(sym) {
  if (sym === _activeSymbol) return;
  _activeSymbol = sym;

  // Update tab appearance
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('tab-btn--active', b.dataset.symbol === sym);
  });

  // Destroy existing chart so it re-renders for new symbol
  if (priceChart) {
    priceChart.destroy();
    priceChart = null;
  }

  resetSymbolSpecificWidgets();
  await loadConfig(sym);
  await refreshSymbolPanel();
}

function resetSymbolSpecificWidgets() {
  document.getElementById('kpi-price').textContent = '–';
  document.getElementById('kpi-signal').textContent = '–';

  const setupEl = document.getElementById('kpi-setup');
  setupEl.textContent = 'WAIT';
  setupEl.className = 'card__value text-gold';

  document.getElementById('kpi-waiting').textContent = '–';

  renderChecks('long-checks', {});
  renderChecks('short-checks', {});
}

// ── Fetch helpers ──────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${url}`);
  return resp.json();
}

// ── Main refresh ──────────────────────────────────────────────────────────

async function refreshAll() {
  await Promise.allSettled([
    refreshGlobal(),
    refreshSymbolPanel(),
    refreshCommonTradePanels(),
    refreshLogs(),
  ]);
}

// ── Global (common) section ───────────────────────────────────────────────

async function refreshGlobal() {
  await Promise.allSettled([
    refreshAllStatus(),
    refreshStats(),
  ]);
}

async function refreshAllStatus() {
  // Fetch all-symbol status dict
  let allStatus;
  try {
    allStatus = await fetchJSON(`${API}/status`);
  } catch { return; }

  // Bot running = any symbol is running
  const anyRunning = Object.values(allStatus).some(s => s.running === 1);
  document.getElementById('bot-indicator').className =
    `indicator ${anyRunning ? 'indicator--running' : 'indicator--stopped'}`;
  document.getElementById('bot-status-text').textContent = anyRunning ? 'Running' : 'Stopped';
  document.getElementById('btn-start').disabled = anyRunning;
  document.getElementById('btn-stop').disabled  = !anyRunning;

  // Equity: sum across all running bots (or show active symbol's)
  const activeStatus = allStatus[_activeSymbol] || {};
  const modeEl = document.getElementById('kpi-mode');
  const mode = String(activeStatus.trading_mode || 'realtrading').toLowerCase();
  modeEl.textContent = mode.toUpperCase();
  modeEl.className = 'card__value ' + modeClass(mode);

  // Equity: use first available
  let equity = null;
  for (const s of Object.values(allStatus)) {
    if (s.equity != null) { equity = s.equity; break; }
  }
  document.getElementById('kpi-equity').textContent =
    equity != null ? `$${Number(equity).toFixed(2)}` : '–';

  // Also update per-symbol status strip for active tab
  updateSymbolStatusStrip(activeStatus);
}

function updateSymbolStatusStrip(s) {
  if (!s) return;

  // Only update price from bot status when the bot has a recorded price.
  // When the bot hasn't run yet (last_price === null), the live price from
  // refreshMarketContext will fill this in instead.
  if (s.last_price) {
    document.getElementById('kpi-price').textContent = formatPrice(s.last_price);
  }

  // kpi-signal reflects the last signal the bot actually executed.
  // 'NONE' is the DB default (bot never fired); display '–' for both cases.
  const sigEl = document.getElementById('kpi-signal');
  const sig = (s.last_signal && s.last_signal !== 'NONE') ? s.last_signal : '–';
  sigEl.textContent = sig;
  sigEl.className = 'card__value ' + signalClass(s.last_signal);

  // kpi-setup and kpi-waiting are owned by refreshMarketContext which
  // computes them live from fresh candles.  Updating them here from bot_status
  // (which is only written during bot ticks) causes a race condition: stale
  // defaults ("WAIT" / "Collecting candles") can overwrite the live values
  // when the two parallel fetches settle in the wrong order.
}

async function refreshStats() {
  // Aggregate stats (all symbols)
  let st;
  try { st = await fetchJSON(`${API}/stats`); } catch { return; }

  const pnlEl = document.getElementById('kpi-pnl');
  const pnl = Number(st.total_pnl || 0);
  pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
  pnlEl.className = 'card__value ' + (pnl >= 0 ? 'text-green' : 'text-red');

  document.getElementById('kpi-winrate').textContent = st.total ? `${st.win_rate}%` : '–';
  document.getElementById('kpi-total-trades').textContent = st.total || 0;
}

// ── Per-symbol section ────────────────────────────────────────────────────

async function refreshSymbolPanel() {
  const sym = _activeSymbol;
  await Promise.allSettled([
    refreshSymbolStatus(sym),
    refreshSymbolStats(sym),
    refreshMarketContext(sym),
  ]);
}

async function refreshSymbolStatus(sym) {
  let s;
  try { s = await fetchJSON(`${API}/status?symbol=${encodeURIComponent(sym)}`); }
  catch { return; }
  if (sym !== _activeSymbol) return;
  updateSymbolStatusStrip(s);
}

async function refreshSymbolStats(sym) {
  let st;
  try { st = await fetchJSON(`${API}/stats?symbol=${encodeURIComponent(sym)}`); }
  catch { return; }
  if (sym !== _activeSymbol) return;

  const symPnlEl = document.getElementById('kpi-sym-pnl');
  const pnl = Number(st.total_pnl || 0);
  symPnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
  symPnlEl.className = 'card__value ' + (pnl >= 0 ? 'text-green' : 'text-red');

  document.getElementById('kpi-sym-wr').textContent = st.total ? `${st.win_rate}%` : '–';
}

async function loadConfig(sym) {
  let cfg;
  try { cfg = await fetchJSON(`${API}/config?symbol=${encodeURIComponent(sym)}`); }
  catch { return; }
  if (sym !== _activeSymbol) return;

  const tbody = document.querySelector('#param-table tbody');
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

async function refreshCommonTradePanels() {
  await refreshTradeHistory();
}

async function refreshTradeHistory() {
  let trades;
  try { trades = await fetchJSON(`${API}/trades?limit=50`); }
  catch { return; }

  const tbody = document.querySelector('#trade-table tbody');
  tbody.innerHTML = '';
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="14" class="empty-msg" style="padding:12px">No trades yet</td></tr>';
    return;
  }

  trades.forEach(t => {
    const pnl = t.pnl != null ? Number(t.pnl) : null;
    const pnlStr = pnl != null
      ? `<span class="${pnl >= 0 ? 'text-green' : 'text-red'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>`
      : '–';
    const dirCls = t.direction === 'LONG' ? 'text-green' : 'text-red';
    const value = Number(t.size) * Number(t.entry_price);

    const entry = Number(t.entry_price);
    const size  = Number(t.size);
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

    tbody.innerHTML += `
      <tr>
        <td>${t.id}</td>
        <td><strong>${t.symbol}</strong></td>
        <td class="${dirCls}"><strong>${t.direction}</strong></td>
        <td>${formatPrice(t.entry_price)}</td>
        <td>${t.exit_price ? formatPrice(t.exit_price) : '–'}</td>
        <td>${t.size}</td>
        <td>$${value.toFixed(2)}</td>
        <td class="text-red">${formatPrice(t.sl_price)}</td>
        <td class="text-green">${formatPrice(t.tp_price)}</td>
        <td>${slLossStr}</td>
        <td>${tpProfitStr}</td>
        <td>${pnlStr}</td>
        <td>${statusBadge(t.status)}</td>
        <td>${fmtTs(t.opened_at)}</td>
      </tr>`;
  });
}

async function refreshMarketContext(sym) {
  let ctx;
  try { ctx = await fetchJSON(`${API}/market/context?symbol=${encodeURIComponent(sym)}&limit=200`); }
  catch { return; }
  if (sym !== _activeSymbol) return;
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

  // Update live-market KPIs from fresh chart data.  This ensures price,
  // setup, and waiting-for are always current even when the bot hasn't
  // run a tick yet (e.g. ETH bot not started).
  if (ctx.values && ctx.values.close != null) {
    document.getElementById('kpi-price').textContent = formatPrice(ctx.values.close);
  }
  if (ctx.diagnostics) {
    const hint = ctx.diagnostics.signal_hint || 'WAIT';
    const setupEl = document.getElementById('kpi-setup');
    setupEl.textContent = hint.replaceAll('_', ' ');
    setupEl.className = 'card__value ' + setupClass(hint);
    document.getElementById('kpi-waiting').textContent =
      ctx.diagnostics.waiting_for || 'Collecting candles';
  }
}

// ── Activity log (common) ─────────────────────────────────────────────────

async function refreshLogs() {
  const container = document.getElementById('log-container');
  let logs;
  try {
    logs = await fetchJSON(`${API}/logs?limit=60`);
  } catch (e) {
    console.error('Activity log fetch failed:', e);
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
    const r = await fetch(`${API}/bot/start`, { method: 'POST' });
    const data = await r.json();
    if (!data.ok) alert(data.message);
    refreshAll();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

async function stopBot() {
  try {
    const r = await fetch(`${API}/bot/stop`, { method: 'POST' });
    const data = await r.json();
    if (!data.ok) alert(data.message);
    refreshAll();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
}

async function clearLogs() {
  if (!confirm('Clear all activity log entries?')) return;
  try {
    const r = await fetch(`${API}/logs/clear`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) { alert(data.message || 'Could not clear log'); return; }
    await refreshLogs();
  } catch (e) { alert('Could not reach API server: ' + e.message); }
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
  return 'text-gold';
}

function renderChecks(containerId, checks) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const items = Object.entries(checks);
  if (!items.length) {
    el.innerHTML = '<li><span>No data</span><span class="check-badge check-badge--no">N/A</span></li>';
    return;
  }
  el.innerHTML = items.map(([name, ok]) => {
    const cls  = ok ? 'check-badge--ok' : 'check-badge--no';
    const text = ok ? 'OK' : 'WAIT';
    return `<li><span>${escHtml(name)}</span><span class="check-badge ${cls}">${text}</span></li>`;
  }).join('');
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
