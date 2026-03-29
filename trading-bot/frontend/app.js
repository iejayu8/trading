/**
 * app.js – Trading Bot Dashboard logic.
 *
 * Polls the Flask API every 5 s to update all panels.
 * Assumes the API server is running on the same host at port 5000.
 */

const API = '/api';

// ── Refresh interval (ms) ──────────────────────────────────────────────────
const POLL_INTERVAL = 5000;

// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  refreshAll();
  setInterval(refreshAll, POLL_INTERVAL);
});

// ── Data fetchers ──────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${url}`);
  return resp.json();
}

// ── Formatting helpers ─────────────────────────────────────────────────────

/** Format a number as a USD price: $12,345.67 */
function formatPrice(value) {
  return `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Format a number with fixed 2 decimal places (no locale grouping). */
function formatFixed2(value) {
  return Number(value).toFixed(2);
}

async function refreshAll() {
  await Promise.allSettled([
    refreshStatus(),
    refreshStats(),
    refreshOpenTrades(),
    refreshTradeHistory(),
    refreshLogs(),
  ]);
}

async function refreshStatus() {
  const s = await fetchJSON(`${API}/status`);

  // Bot running indicator
  const running = s.running === 1;
  document.getElementById('bot-indicator').className =
    `indicator ${running ? 'indicator--running' : 'indicator--stopped'}`;
  document.getElementById('bot-status-text').textContent = running ? 'Running' : 'Stopped';
  document.getElementById('btn-start').disabled = running;
  document.getElementById('btn-stop').disabled  = !running;

  // KPI cards
  document.getElementById('kpi-symbol').textContent  = s.symbol || '–';
  document.getElementById('kpi-price').textContent   =
    s.last_price ? formatPrice(s.last_price) : '–';

  const sigEl = document.getElementById('kpi-signal');
  sigEl.textContent = s.last_signal || '–';
  sigEl.className = 'card__value ' + signalClass(s.last_signal);

  const setupEl = document.getElementById('kpi-setup');
  const hint = s.signal_hint || 'WAIT';
  setupEl.textContent = hint.replaceAll('_', ' ');
  setupEl.className = 'card__value ' + setupClass(hint);

  const waitingEl = document.getElementById('kpi-waiting');
  waitingEl.textContent = s.waiting_for || 'Collecting candles';
  waitingEl.className = 'card__value card__value--small';

  document.getElementById('kpi-equity').textContent =
    s.equity ? `$${Number(s.equity).toFixed(2)}` : '–';

  const mode = String(s.trading_mode || '').toLowerCase();
  if (mode) {
    const modeEl = document.getElementById('kpi-mode');
    modeEl.textContent = mode.toUpperCase();
    modeEl.className = 'card__value ' + modeClass(mode);
  }
}

async function refreshStats() {
  const st = await fetchJSON(`${API}/stats`);

  const pnlEl = document.getElementById('kpi-pnl');
  const pnl = Number(st.total_pnl || 0);
  pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
  pnlEl.className = 'card__value ' + (pnl >= 0 ? 'text-green' : 'text-red');

  document.getElementById('kpi-winrate').textContent =
    st.total ? `${st.win_rate}%` : '–';
}

async function loadConfig() {
  const cfg = await fetchJSON(`${API}/config`);
  const tbody = document.querySelector('#param-table tbody');
  tbody.innerHTML = '';

  const mode = String(cfg.trading_mode || 'realtrading').toLowerCase();
  const modeEl = document.getElementById('kpi-mode');
  modeEl.textContent = mode.toUpperCase();
  modeEl.className = 'card__value ' + modeClass(mode);

  const rows = [
    ['Symbol',           cfg.symbol],
    ['Trading Mode',     mode],
    ['Timeframe',        cfg.timeframe],
    ['Leverage',         `${cfg.leverage}×`],
    ['Risk / Trade',     `${cfg.risk_per_trade_pct}%`],
    ['Stop Loss',        `${cfg.stop_loss_pct}%`],
    ['Take Profit',      `${cfg.take_profit_pct}%`],
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

async function refreshOpenTrades() {
  const trades = await fetchJSON(`${API}/trades/open`);
  const container = document.getElementById('open-trades-container');
  container.innerHTML = '';

  if (!trades.length) {
    container.innerHTML = '<p class="empty-msg">No open positions</p>';
    return;
  }

  trades.forEach(t => {
    const dir = t.direction;
    const pnlCls = dir === 'LONG' ? 'text-green' : 'text-red';
    container.innerHTML += `
      <div class="position-card">
        <div class="direction ${pnlCls}">${dir} ${t.symbol}</div>
        <div class="pos-row"><label>Entry</label><span>${formatPrice(t.entry_price)}</span></div>
        <div class="pos-row"><label>Size</label><span>${t.size} BTC</span></div>
        <div class="pos-row"><label>SL</label><span class="text-red">${formatPrice(t.sl_price)}</span></div>
        <div class="pos-row"><label>TP</label><span class="text-green">${formatPrice(t.tp_price)}</span></div>
        <div class="pos-row"><label>Leverage</label><span>${t.leverage}×</span></div>
        <div class="pos-row"><label>Opened</label><span>${fmtTs(t.opened_at)}</span></div>
      </div>`;
  });
}

async function refreshTradeHistory() {
  const trades = await fetchJSON(`${API}/trades?limit=50`);
  const tbody = document.querySelector('#trade-table tbody');
  tbody.innerHTML = '';

  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-msg" style="padding:12px">No trades yet</td></tr>';
    return;
  }

  trades.forEach(t => {
    const pnl = t.pnl != null ? Number(t.pnl) : null;
    const pnlStr = pnl != null
      ? `<span class="${pnl >= 0 ? 'text-green' : 'text-red'}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>`
      : '–';
    const dirCls = t.direction === 'LONG' ? 'text-green' : 'text-red';
    tbody.innerHTML += `
      <tr>
        <td>${t.id}</td>
        <td class="${dirCls}"><strong>${t.direction}</strong></td>
        <td>${formatPrice(t.entry_price)}</td>
        <td>${t.exit_price ? formatPrice(t.exit_price) : '–'}</td>
        <td>${t.size}</td>
        <td class="text-red">${formatPrice(t.sl_price)}</td>
        <td class="text-green">${formatPrice(t.tp_price)}</td>
        <td>${pnlStr}</td>
        <td>${statusBadge(t.status)}</td>
        <td>${fmtTs(t.opened_at)}</td>
      </tr>`;
  });
}

async function refreshLogs() {
  const logs = await fetchJSON(`${API}/logs?limit=60`);
  const container = document.getElementById('log-container');
  container.innerHTML = '';

  // API returns newest first; show oldest → newest so latest is at the bottom.
  const orderedLogs = [...logs].reverse();

  orderedLogs.forEach(l => {
    const ts = fmtTs(l.ts);
    container.innerHTML += `
      <div class="log-entry">
        <span class="log-ts">${ts}</span>
        <span class="log-level-${l.level}">[${l.level}]</span>
        <span>${escHtml(l.message)}</span>
      </div>`;
  });

  // Always keep the viewport on the most recent event.
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
    if (!r.ok || !data.ok) {
      alert(data.message || 'Could not clear activity log');
      return;
    }
    await refreshLogs();
  } catch (e) {
    alert('Could not reach API server: ' + e.message);
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

function signalClass(sig) {
  if (sig === 'LONG')  return 'text-green';
  if (sig === 'SHORT') return 'text-red';
  return '';
}

function setupClass(hint) {
  if (hint === 'LONG_READY') return 'text-green';
  if (hint === 'SHORT_READY') return 'text-red';
  if (hint === 'COOLDOWN') return 'text-blue';
  return 'text-gold';
}

function modeClass(mode) {
  if (mode === 'papertrading') return 'text-blue';
  if (mode === 'realtrading') return 'text-green';
  return 'text-gold';
}

function statusBadge(status) {
  const cls = status === 'OPEN' ? 'text-blue' : (status === 'CLOSED' ? 'text-muted' : '');
  return `<span class="${cls}">${status}</span>`;
}

function fmtTs(ts) {
  if (!ts) return '–';

  // Normalize timestamps from backend/SQLite:
  // - If timezone is present (Z or ±HH:MM), keep as-is.
  // - If timezone is missing, treat as UTC to avoid local/UTC drift.
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
