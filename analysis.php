<?php
/**
 * Deriv Bot Session Dashboard
 *
 * Place in the project root (same level as data/)
 * Run with: php -S 0.0.0.0:8080 analysis.php
 *
 * API endpoints:
 *   ?api=sessions         → list all session JSONs with summary
 *   ?api=session&file=X   → return one full session JSON
 */

$DATA_DIR = __DIR__ . '/data';

// ── API ─────────────────────────────────────────────────────────────────────
if (isset($_GET['api'])) {
    header('Content-Type: application/json');
    header('Cache-Control: no-cache');

    // List all sessions
    if ($_GET['api'] === 'sessions') {
        $files = glob($DATA_DIR . '/*.json');
        if (!$files) $files = [];
        $sessions = [];
        foreach ($files as $f) {
            $raw = @file_get_contents($f);
            if (!$raw) continue;
            $d = json_decode($raw, true);
            if (!$d) continue;
            $sess = $d['session'] ?? [];
            $sum  = $d['summary'] ?? [];
            $sessions[] = [
                'file'           => basename($f),
                'started_at'     => $sess['started_at'] ?? null,
                'updated_at'     => $sess['updated_at'] ?? null,
                'account_mode'   => $sess['account_mode'] ?? 'demo',
                'account_loginid'=> $sess['account_loginid'] ?? '',
                'account_name'   => trim($sess['account_fullname'] ?? ''),
                'currency'       => $sess['account_currency'] ?? 'USD',
                'initial_equity' => $sess['initial_equity'] ?? 0,
                'current_equity' => $sess['current_equity'] ?? 0,
                'base_stake'     => $sess['base_stake'] ?? 0,
                'profit_target'  => $sess['profit_target'] ?? 0,
                'loss_limit'     => $sess['loss_limit'] ?? 0,
                'trade_count'    => $sum['trade_count'] ?? 0,
                'wins'           => $sum['wins'] ?? 0,
                'losses'         => $sum['losses'] ?? 0,
                'net_pnl'        => $sum['net_pnl'] ?? 0,
                'win_rate'       => $sum['win_rate'] ?? 0,
                'is_live'        => ($sess['active_contract'] !== null),
            ];
        }
        usort($sessions, fn($a, $b) => ($b['started_at'] ?? 0) <=> ($a['started_at'] ?? 0));
        echo json_encode($sessions);
        exit;
    }

    // Single session
    if ($_GET['api'] === 'session' && isset($_GET['file'])) {
        $file = basename($_GET['file']);
        $path = $DATA_DIR . '/' . $file;
        if (!file_exists($path)) {
            http_response_code(404);
            echo json_encode(['error' => 'Not found']);
            exit;
        }
        echo file_get_contents($path);
        exit;
    }

    http_response_code(400);
    echo json_encode(['error' => 'Unknown endpoint']);
    exit;
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Deriv Bot Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0b0f;--bg2:#12131a;--bg3:#1a1b25;--bg4:#222333;
  --border:#2a2b3d;--border2:#3a3b5d;
  --text:#e2e4f0;--text2:#9395aa;--text3:#666879;
  --green:#00e676;--green2:#00c85320;--green3:#00c853;
  --red:#ff5252;--red2:#ff525220;--red3:#d32f2f;
  --amber:#ffab40;--amber2:#ffab4020;
  --blue:#448aff;--blue2:#448aff20;
  --purple:#b388ff;--cyan:#18ffff;
  --font:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;
  --radius:10px;--radius-sm:6px;--radius-lg:16px;
}
html{font-size:14px}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:9999;pointer-events:none;opacity:.025;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.container{max-width:1400px;margin:0 auto;padding:24px 20px}

/* ── HEADER ── */
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;padding-bottom:20px;border-bottom:1px solid var(--border)}
.header h1{font-size:1.7rem;font-weight:800;letter-spacing:-.02em;background:linear-gradient(135deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .subtitle{color:var(--text2);font-size:.85rem;margin-top:3px}
.header-actions{display:flex;gap:10px;align-items:center}
.auto-refresh-label{color:var(--text2);font-size:.78rem;display:flex;align-items:center;gap:6px;cursor:pointer}
.auto-refresh-label input{accent-color:var(--green)}

/* ── BUTTONS ── */
.btn{font-family:var(--font);font-size:.82rem;font-weight:600;padding:8px 18px;border-radius:var(--radius-sm);border:none;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none!important}
.btn-ghost{background:var(--bg3);color:var(--text);border:1px solid var(--border)}
.btn-ghost:hover:not(:disabled){background:var(--bg4);border-color:var(--border2)}

/* ── SESSION GRID ── */
.sessions-panel{margin-bottom:28px}
.sessions-panel h2{font-size:.75rem;font-weight:600;color:var(--text2);margin-bottom:12px;text-transform:uppercase;letter-spacing:.08em}
.session-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.session-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px;cursor:pointer;transition:all .15s;position:relative;overflow:hidden}
.session-card:hover{border-color:var(--border2);transform:translateY(-2px);box-shadow:0 8px 30px #00000040}
.session-card.active-card{border-color:var(--green);box-shadow:0 0 0 1px var(--green),0 8px 30px #00e67615}
.session-card.live-card::before{content:'';position:absolute;top:12px;right:12px;width:8px;height:8px;background:var(--green);border-radius:50%;animation:pulse-live 2s infinite}
@keyframes pulse-live{0%,100%{opacity:1;box-shadow:0 0 0 0 #00e67660}50%{opacity:.7;box-shadow:0 0 0 6px #00e67600}}
.sc-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px}
.sc-id{font-family:var(--mono);font-size:.88rem;font-weight:700;color:var(--text)}
.sc-badge{font-size:.65rem;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.05em}
.sc-badge.demo{background:var(--blue2);color:var(--blue)}
.sc-badge.live-badge{background:var(--green2);color:var(--green)}
.sc-date{font-size:.73rem;color:var(--text2);margin-bottom:10px}
.sc-stats{display:flex;gap:14px;font-size:.78rem}
.sc-stat{display:flex;flex-direction:column;gap:2px}
.sc-stat span:first-child{color:var(--text3);font-size:.65rem;text-transform:uppercase;letter-spacing:.05em}
.sc-stat span:last-child{font-family:var(--mono);font-weight:600}
.c-green{color:var(--green)}.c-red{color:var(--red)}.c-amber{color:var(--amber)}

/* ── EMPTY STATE ── */
.empty-state{text-align:center;padding:80px 20px;color:var(--text2)}
.empty-state .big-icon{font-size:3rem;margin-bottom:16px;opacity:.5}
.empty-state p{font-size:.95rem;max-width:400px;margin:0 auto;line-height:1.6}

/* ── DASHBOARD ── */
.dashboard{display:none}
.dashboard.visible{display:block}

/* ── CONFIG BAR ── */
.config-bar{display:flex;gap:20px;flex-wrap:wrap;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 18px;margin-bottom:20px;align-items:center}
.config-item{display:flex;flex-direction:column;gap:2px}
.config-item .clabel{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3)}
.config-item .cval{font-family:var(--mono);font-size:.85rem;font-weight:600}
.config-symbols{display:flex;gap:5px;flex-wrap:wrap}
.sym-tag{font-family:var(--mono);font-size:.68rem;padding:2px 7px;border-radius:4px;background:var(--bg4);border:1px solid var(--border2);color:var(--text2)}

/* ── STAT CARDS ── */
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
.stat-card .label{font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:6px}
.stat-card .value{font-family:var(--mono);font-size:1.35rem;font-weight:700;line-height:1}
.stat-card .sub{font-size:.7rem;color:var(--text2);margin-top:5px}
.stat-card.hl-green{border-color:var(--green);background:linear-gradient(135deg,#00e67608,#00e67602)}
.stat-card.hl-red{border-color:var(--red);background:linear-gradient(135deg,#ff525208,#ff525202)}

/* ── CHARTS ── */
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
@media(max-width:900px){.chart-row{grid-template-columns:1fr}}
.chart-section h3{font-size:.73rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:10px;font-weight:600}
.chart-container{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
canvas{width:100%!important;max-height:280px}

/* ── STREAKS ── */
.streak-bar{display:flex;width:100%;height:36px;border-radius:var(--radius-sm);overflow:hidden;background:var(--bg3);margin-bottom:16px}
.streak-seg{display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:.68rem;font-weight:700;min-width:14px;position:relative;color:#fff;transition:opacity .15s}
.streak-seg.win{background:var(--green3)}.streak-seg.loss{background:var(--red3)}
.streak-seg:hover{opacity:.8}
.streak-tip{position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:var(--bg4);border:1px solid var(--border2);border-radius:var(--radius-sm);padding:6px 10px;font-size:.68rem;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .15s;z-index:10}
.streak-seg:hover .streak-tip{opacity:1}
.streak-section{margin-bottom:24px}
.streak-table{width:100%;border-collapse:collapse}
.streak-table th{font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);text-align:left;padding:8px 12px;font-weight:600;border-bottom:1px solid var(--border)}
.streak-table td{padding:8px 12px;font-family:var(--mono);font-size:.8rem;border-bottom:1px solid var(--border)}
.streak-table tr:hover td{background:var(--bg3)}

/* ── SYMBOL BREAKDOWN ── */
.symbol-section{margin-bottom:24px}
.symbol-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.sym-card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.sym-card .sym-name{font-family:var(--mono);font-size:.9rem;font-weight:700;margin-bottom:8px}
.sym-card .sym-stats{display:flex;gap:12px;font-size:.75rem}
.sym-stat{display:flex;flex-direction:column;gap:2px}
.sym-stat span:first-child{color:var(--text3);font-size:.62rem;text-transform:uppercase;letter-spacing:.04em}
.sym-stat span:last-child{font-family:var(--mono);font-weight:600}
.sym-bar-wrap{margin-top:10px;background:var(--bg3);border-radius:4px;overflow:hidden;height:5px}
.sym-bar-fill{height:100%;background:var(--green);transition:width .4s}

/* ── TRADE LOG ── */
.trade-log{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:24px}
.trade-log-header{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.trade-log-header h3{font-size:.85rem;font-weight:600}
.trade-count{font-family:var(--mono);font-size:.75rem;color:var(--text2)}
.trade-table-wrap{overflow-x:auto;max-height:480px;overflow-y:auto}
.trade-table{width:100%;border-collapse:collapse;min-width:720px}
.trade-table th{position:sticky;top:0;z-index:2;font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);text-align:left;padding:10px 14px;font-weight:600;background:var(--bg2);border-bottom:1px solid var(--border)}
.trade-table td{padding:8px 14px;font-family:var(--mono);font-size:.77rem;border-bottom:1px solid var(--border)}
.trade-table tbody tr:hover td{background:var(--bg3)}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.68rem;font-weight:700;letter-spacing:.02em}
.badge-win{background:var(--green2);color:var(--green)}.badge-loss{background:var(--red2);color:var(--red)}
.badge-odd{background:#b388ff20;color:var(--purple)}.badge-even{background:var(--blue2);color:var(--blue)}

/* ── MISC ── */
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}::-webkit-scrollbar-thumb:hover{background:var(--border2)}
.spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--green);border-radius:50%;animation:spin .6s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-overlay{position:fixed;inset:0;background:#0a0b0fdd;display:flex;align-items:center;justify-content:center;z-index:100;flex-direction:column;gap:16px;transition:opacity .3s}
.loading-overlay.hidden{opacity:0;pointer-events:none}
.loading-overlay p{color:var(--text2);font-size:.85rem}
@media(max-width:640px){
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .session-grid{grid-template-columns:1fr}
  .config-bar{gap:12px}
  .header{flex-direction:column;align-items:flex-start;gap:12px}
}
</style>
</head>
<body>

<div class="loading-overlay" id="loader">
  <div class="spinner" style="width:32px;height:32px;border-width:3px"></div>
  <p>Loading sessions...</p>
</div>

<div class="container">

  <!-- HEADER -->
  <div class="header">
    <div>
      <h1>DERIV BOT DASHBOARD</h1>
      <div class="subtitle">Multi-Symbol Digit Strategy Monitor</div>
    </div>
    <div class="header-actions">
      <label class="auto-refresh-label">
        <input type="checkbox" id="autoRefresh"> Auto-refresh 10s
      </label>
      <button class="btn btn-ghost" onclick="refreshAll()">
        <span>&#8635;</span> Refresh
      </button>
    </div>
  </div>

  <!-- SESSION CARDS -->
  <div class="sessions-panel">
    <h2>Sessions <span id="sessionCount" style="color:var(--text3);font-weight:400"></span></h2>
    <div class="session-grid" id="sessionGrid"></div>
  </div>

  <!-- EMPTY STATE -->
  <div class="empty-state" id="emptyState">
    <div class="big-icon">&#128202;</div>
    <p>Select a session above to view detailed stats, trade history, equity curve, and symbol breakdown.</p>
  </div>

  <!-- DASHBOARD -->
  <div class="dashboard" id="dashboard">

    <!-- CONFIG BAR -->
    <div class="config-bar" id="configBar"></div>

    <!-- STAT CARDS -->
    <div class="stats-row" id="statsRow"></div>

    <!-- CHARTS ROW 1 -->
    <div class="chart-row">
      <div class="chart-section">
        <h3>Equity Curve</h3>
        <div class="chart-container"><canvas id="equityChart"></canvas></div>
      </div>
      <div class="chart-section">
        <h3>Cumulative P&amp;L</h3>
        <div class="chart-container"><canvas id="pnlChart"></canvas></div>
      </div>
    </div>

    <!-- CHARTS ROW 2 -->
    <div class="chart-row">
      <div class="chart-section">
        <h3>Stake Progression</h3>
        <div class="chart-container"><canvas id="stakeChart"></canvas></div>
      </div>
      <div class="chart-section">
        <h3>Win / Loss Distribution</h3>
        <div class="chart-container"><canvas id="wlChart"></canvas></div>
      </div>
    </div>

    <!-- STREAKS -->
    <div class="streak-section">
      <h3 style="font-size:.73rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:10px;font-weight:600">Win / Loss Streak Map</h3>
      <div class="chart-container">
        <div class="streak-bar" id="streakBar"></div>
        <table class="streak-table">
          <thead><tr><th>Type</th><th>Length</th><th>From</th><th>To</th><th>P&amp;L Impact</th><th>Time</th></tr></thead>
          <tbody id="streakBody"></tbody>
        </table>
      </div>
    </div>

    <!-- SYMBOL BREAKDOWN -->
    <div class="symbol-section">
      <h3 style="font-size:.73rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:10px;font-weight:600">Symbol Breakdown</h3>
      <div class="symbol-grid" id="symbolGrid"></div>
    </div>

    <!-- TRADE LOG -->
    <div class="trade-log">
      <div class="trade-log-header">
        <h3>Trade Log</h3>
        <span class="trade-count" id="tradeCount"></span>
      </div>
      <div class="trade-table-wrap">
        <table class="trade-table">
          <thead>
            <tr>
              <th>#</th><th>Time</th><th>Symbol</th><th>Type</th>
              <th>Result</th><th>Stake</th><th>Profit</th><th>Payout</th>
              <th>Cum. P&amp;L</th><th>Equity</th>
            </tr>
          </thead>
          <tbody id="tradeBody"></tbody>
        </table>
      </div>
    </div>

  </div><!-- /dashboard -->
</div><!-- /container -->

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
// ─── STATE ───────────────────────────────────────────────────────────────────
let sessions = [];
let activeFile = null;
let activeData = null;
let charts = {};
let autoTimer = null;

// ─── API ─────────────────────────────────────────────────────────────────────
async function apiFetch(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
async function init() {
  await loadSessions();
  document.getElementById('loader').classList.add('hidden');
}

async function loadSessions() {
  try {
    sessions = await apiFetch('?api=sessions');
    renderSessionGrid();
    if (activeFile) {
      const still = sessions.find(s => s.file === activeFile);
      if (still) await loadSession(activeFile, false);
    }
  } catch(e) { console.error('Sessions error:', e); }
}

function refreshAll() { loadSessions(); }

// ─── AUTO REFRESH ─────────────────────────────────────────────────────────────
document.getElementById('autoRefresh').addEventListener('change', function() {
  clearInterval(autoTimer);
  if (this.checked) autoTimer = setInterval(refreshAll, 10000);
});

// ─── SESSION GRID ─────────────────────────────────────────────────────────────
function renderSessionGrid() {
  const grid = document.getElementById('sessionGrid');
  const countEl = document.getElementById('sessionCount');
  countEl.textContent = sessions.length ? `(${sessions.length})` : '';

  if (!sessions.length) {
    grid.innerHTML = '<div style="color:var(--text2);padding:20px;font-size:.85rem">No JSON files found in the data/ folder.</div>';
    return;
  }

  grid.innerHTML = sessions.map(s => {
    const pnl = s.net_pnl ?? 0;
    const pnlClass = pnl >= 0 ? 'c-green' : 'c-red';
    const pnlSign  = pnl >= 0 ? '+' : '';
    const wr = ((s.win_rate ?? 0) * 100).toFixed(1);
    const modeBadge = s.account_mode === 'live'
      ? '<span class="sc-badge live-badge">Live</span>'
      : '<span class="sc-badge demo">Demo</span>';
    const isLive = s.is_live;
    return `
    <div class="session-card ${isLive ? 'live-card' : ''} ${activeFile === s.file ? 'active-card' : ''}"
         data-file="${s.file}" onclick="loadSession('${s.file}')">
      <div class="sc-top">
        <div class="sc-id">${s.file.replace('.json','')}</div>
        ${modeBadge}
      </div>
      <div class="sc-date">${fmtTs(s.started_at)} ${isLive ? '&bull; <span style="color:var(--green)">LIVE</span>' : ''}</div>
      <div class="sc-stats">
        <div class="sc-stat"><span>Trades</span><span>${s.trade_count}</span></div>
        <div class="sc-stat"><span>Win Rate</span><span>${wr}%</span></div>
        <div class="sc-stat"><span>W / L</span><span>${s.wins} / ${s.losses}</span></div>
        <div class="sc-stat"><span>P&amp;L</span><span class="${pnlClass}">${pnlSign}$${pnl.toFixed(2)}</span></div>
      </div>
    </div>`;
  }).join('');
}

// ─── LOAD ONE SESSION ─────────────────────────────────────────────────────────
async function loadSession(file, scroll = true) {
  activeFile = file;
  try {
    activeData = await apiFetch('?api=session&file=' + encodeURIComponent(file));
    renderDashboard();
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('dashboard').classList.add('visible');
    document.querySelectorAll('.session-card').forEach(c =>
      c.classList.toggle('active-card', c.dataset.file === file));
    if (scroll) document.getElementById('dashboard').scrollIntoView({ behavior:'smooth', block:'start' });
  } catch(e) { console.error('Session load error:', e); }
}

// ─── RENDER DASHBOARD ─────────────────────────────────────────────────────────
function renderDashboard() {
  if (!activeData) return;
  const d      = activeData;
  const sess   = d.session   || {};
  const sum    = d.summary   || {};
  const trades = d.trades    || [];
  const curve  = d.equity_curve || [];

  // ── Config bar ──
  document.getElementById('configBar').innerHTML = `
    <div class="config-item"><span class="clabel">Account</span><span class="cval">${sess.account_loginid||'—'}</span></div>
    <div class="config-item"><span class="clabel">Name</span><span class="cval" style="font-size:.78rem">${sess.account_fullname?.trim()||'—'}</span></div>
    <div class="config-item"><span class="clabel">Mode</span><span class="cval" style="color:${sess.account_mode==='live'?'var(--green)':'var(--blue)'};text-transform:capitalize">${sess.account_mode||'demo'}</span></div>
    <div class="config-item"><span class="clabel">Base Stake</span><span class="cval">$${sess.base_stake??'—'}</span></div>
    <div class="config-item"><span class="clabel">Profit Target</span><span class="cval" style="color:var(--green)">$${sess.profit_target??'—'}</span></div>
    <div class="config-item"><span class="clabel">Loss Limit</span><span class="cval" style="color:var(--red)">$${sess.loss_limit??'—'}</span></div>
    <div class="config-item"><span class="clabel">Score Threshold</span><span class="cval">${sess.score_threshold??'—'}</span></div>
    <div class="config-item"><span class="clabel">Duration</span><span class="cval">${sess.duration??'—'} ${sess.duration_unit||''}</span></div>
    <div class="config-item"><span class="clabel">Started</span><span class="cval" style="font-size:.75rem">${fmtTs(sess.started_at)}</span></div>
    <div class="config-item"><span class="clabel">Symbols</span><span class="cval"><div class="config-symbols">${(sess.symbols||[]).map(s=>`<span class="sym-tag">${s}</span>`).join('')}</div></span></div>
  `;

  // ── Compute derived stats ──
  const initEq   = sess.initial_equity || 0;
  const curEq    = sess.current_equity || 0;
  const eqChange = curEq - initEq;
  const pnl      = sum.net_pnl ?? 0;
  const wr       = (sum.win_rate ?? 0) * 100;

  // Peak equity & max drawdown from equity_curve
  let peakEq = initEq, maxDD = 0, runPeak = initEq;
  for (const pt of curve) {
    if (pt.equity > runPeak) runPeak = pt.equity;
    const dd = runPeak - pt.equity;
    if (dd > maxDD) maxDD = dd;
  }
  peakEq = runPeak;

  // Avg win / avg loss
  const winTrades  = trades.filter(t => t.result === 'win');
  const lossTrades = trades.filter(t => t.result === 'loss');
  const avgWin  = winTrades.length  ? winTrades.reduce((a,t)=>a+t.profit,0)/winTrades.length : 0;
  const avgLoss = lossTrades.length ? lossTrades.reduce((a,t)=>a+Math.abs(t.profit),0)/lossTrades.length : 0;

  // Streaks
  const streaks = computeStreaks(trades);
  const maxWS = Math.max(0, ...streaks.filter(s=>s.type==='win').map(s=>s.length));
  const maxLS = Math.max(0, ...streaks.filter(s=>s.type==='loss').map(s=>s.length));

  const duration = computeDuration(sess.started_at, sess.updated_at);

  const pnlClass = pnl >= 0 ? 'c-green' : 'c-red';
  const eqClass  = eqChange >= 0 ? 'c-green' : 'c-red';
  const hlClass  = pnl >= 0 ? 'hl-green' : 'hl-red';

  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card ${hlClass}">
      <div class="label">Net P&amp;L</div>
      <div class="value ${pnlClass}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div>
      <div class="sub">${sum.trade_count||0} trades</div>
    </div>
    <div class="stat-card">
      <div class="label">Win Rate</div>
      <div class="value">${wr.toFixed(1)}%</div>
      <div class="sub">${sum.wins||0}W &nbsp;/&nbsp; ${sum.losses||0}L</div>
    </div>
    <div class="stat-card">
      <div class="label">Equity</div>
      <div class="value">$${curEq.toFixed(2)}</div>
      <div class="sub ${eqClass}">${eqChange>=0?'+':''}$${eqChange.toFixed(2)} from start</div>
    </div>
    <div class="stat-card">
      <div class="label">Peak Equity</div>
      <div class="value c-green">$${peakEq.toFixed(2)}</div>
      <div class="sub">+$${(peakEq-initEq).toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Max Drawdown</div>
      <div class="value c-red">-$${maxDD.toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Avg Win / Loss</div>
      <div class="value" style="font-size:1rem">
        <span class="c-green">$${avgWin.toFixed(2)}</span>
        <span style="color:var(--text3)"> / </span>
        <span class="c-red">$${avgLoss.toFixed(2)}</span>
      </div>
    </div>
    <div class="stat-card">
      <div class="label">Best / Worst Streak</div>
      <div class="value" style="font-size:1rem">
        <span class="c-green">${maxWS}W</span>
        <span style="color:var(--text3)"> / </span>
        <span class="c-red">${maxLS}L</span>
      </div>
    </div>
    <div class="stat-card">
      <div class="label">Duration</div>
      <div class="value" style="font-size:1.05rem">${duration}</div>
      <div class="sub">Score thr: ${sess.score_threshold??'—'}</div>
    </div>
  `;

  renderCharts(trades, curve, initEq);
  renderStreaks(streaks, trades);
  renderSymbolBreakdown(trades);

  // Trade log
  document.getElementById('tradeCount').textContent = `${trades.length} trades`;
  let cumPnl = 0;
  document.getElementById('tradeBody').innerHTML = trades.map(t => {
    cumPnl += t.profit;
    const isWin = t.result === 'win';
    const pc = isWin ? 'c-green' : 'c-red';
    const pSign = t.profit >= 0 ? '+' : '';
    const cpSign = cumPnl >= 0 ? '+' : '';
    const typeBadge = t.contract_type.includes('ODD')
      ? '<span class="badge badge-odd">ODD</span>'
      : '<span class="badge badge-even">EVEN</span>';
    return `<tr>
      <td>${t.trade_no}</td>
      <td style="font-size:.7rem">${fmtTs(t.timestamp)}</td>
      <td><span class="sym-tag" style="font-size:.68rem">${t.symbol}</span></td>
      <td>${typeBadge}</td>
      <td><span class="badge ${isWin?'badge-win':'badge-loss'}">${t.result.toUpperCase()}</span></td>
      <td>$${t.stake.toFixed(2)}</td>
      <td class="${pc}">${pSign}$${t.profit.toFixed(2)}</td>
      <td>$${t.payout.toFixed(2)}</td>
      <td style="color:${cumPnl>=0?'var(--green)':'var(--red)'}">${cpSign}$${cumPnl.toFixed(2)}</td>
      <td>$${t.equity_after.toFixed(2)}</td>
    </tr>`;
  }).join('');
}

// ─── CHARTS ──────────────────────────────────────────────────────────────────
function destroyCharts() { Object.values(charts).forEach(c => c.destroy()); charts = {}; }

const TICK = '#9395aa';
const FONT_MONO = 'JetBrains Mono';

function baseOpts() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        titleColor: '#e2e4f0', bodyColor: '#e2e4f0',
        backgroundColor: '#1a1b25ee', borderColor: '#3a3b5d', borderWidth: 1,
        titleFont: { family: FONT_MONO, size: 11 },
        bodyFont:  { family: FONT_MONO, size: 11 },
      }
    },
    scales: {
      x: { grid:{ color:'#2a2b3d40' }, ticks:{ color:TICK, font:{ family:FONT_MONO, size:10 } } },
      y: { grid:{ color:'#2a2b3d40' }, ticks:{ color:TICK, font:{ family:FONT_MONO, size:10 } } }
    }
  };
}

function dollarTicks(opts) {
  opts.scales.y.ticks.callback = v => '$' + v.toFixed(2);
  return opts;
}

function renderCharts(trades, curve, initEq) {
  destroyCharts();
  if (!trades.length) return;

  const labels = trades.map(t => '#' + t.trade_no);
  const ptColors = trades.map(t => t.result === 'win' ? '#00e676' : '#ff5252');

  // ── Equity curve (from equity_curve array) ──
  const curveLabels = curve.map(p => '#' + p.trade_no);
  const curveVals   = curve.map(p => p.equity);
  const eqCtx = document.getElementById('equityChart').getContext('2d');
  const eqGrad = eqCtx.createLinearGradient(0,0,0,280);
  eqGrad.addColorStop(0,'#18ffff30'); eqGrad.addColorStop(1,'#18ffff00');
  charts.equity = new Chart(eqCtx, {
    type: 'line',
    data: { labels: curveLabels, datasets: [{ data: curveVals, borderColor: '#18ffff', backgroundColor: eqGrad, fill: true, tension: .3, pointRadius: curve.length > 60 ? 0 : 3, borderWidth: 2 }] },
    options: dollarTicks({ ...baseOpts(), plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, callbacks: { label: ctx => `Equity: $${ctx.parsed.y.toFixed(2)}` }}}})
  });

  // ── Cumulative P&L ──
  let cum = 0;
  const pnlVals = trades.map(t => { cum += t.profit; return cum; });
  const finalPnl = pnlVals[pnlVals.length - 1];
  const pnlCtx = document.getElementById('pnlChart').getContext('2d');
  const pnlGrad = pnlCtx.createLinearGradient(0,0,0,280);
  if (finalPnl >= 0) { pnlGrad.addColorStop(0,'#00e67640'); pnlGrad.addColorStop(1,'#00e67600'); }
  else               { pnlGrad.addColorStop(0,'#ff525200'); pnlGrad.addColorStop(1,'#ff525230'); }
  charts.pnl = new Chart(pnlCtx, {
    type: 'line',
    data: { labels, datasets: [{ data: pnlVals, borderColor: finalPnl >= 0 ? '#00e676' : '#ff5252', backgroundColor: pnlGrad, fill: true, tension: .3, pointRadius: trades.length > 60 ? 0 : 3, pointBackgroundColor: ptColors, borderWidth: 2 }] },
    options: dollarTicks({ ...baseOpts(), plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, callbacks: { label: ctx => `P&L: $${ctx.parsed.y.toFixed(2)} (${trades[ctx.dataIndex].result})` }}}} )
  });

  // ── Stake bar ──
  const stakeVals   = trades.map(t => t.stake);
  const stakeColors = trades.map(t => t.result === 'win' ? '#00e67680' : '#ff525280');
  const stakeBorder = trades.map(t => t.result === 'win' ? '#00e676' : '#ff5252');
  charts.stake = new Chart(document.getElementById('stakeChart').getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [{ data: stakeVals, backgroundColor: stakeColors, borderColor: stakeBorder, borderWidth: 1, borderRadius: 3 }] },
    options: dollarTicks(baseOpts())
  });

  // ── Win/Loss donut ──
  const wins = trades.filter(t => t.result === 'win').length;
  charts.wl = new Chart(document.getElementById('wlChart').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: ['Wins', 'Losses'],
      datasets: [{ data: [wins, trades.length - wins], backgroundColor: ['#00e67690','#ff525290'], borderColor: ['#00e676','#ff5252'], borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { color: TICK, font:{ family:'Outfit', size:12 }, padding:16 }}}
    }
  });
}

// ─── STREAKS ─────────────────────────────────────────────────────────────────
function computeStreaks(trades) {
  if (!trades.length) return [];
  const out = [];
  let cur = { type: trades[0].result, start: 0, length: 1, pnl: trades[0].profit };
  for (let i = 1; i < trades.length; i++) {
    if (trades[i].result === cur.type) { cur.length++; cur.pnl += trades[i].profit; }
    else { cur.end = i - 1; out.push({...cur}); cur = { type: trades[i].result, start: i, length: 1, pnl: trades[i].profit }; }
  }
  cur.end = trades.length - 1; out.push(cur);
  return out;
}

function renderStreaks(streaks, trades) {
  const total = trades.length || 1;
  document.getElementById('streakBar').innerHTML = streaks.map(sk => {
    const pct = (sk.length / total) * 100;
    const sign = sk.pnl >= 0 ? '+' : '';
    return `<div class="streak-seg ${sk.type}" style="width:${Math.max(pct, 1.5)}%">${sk.length > 2 ? sk.length : ''}<div class="streak-tip">${sk.type.toUpperCase()} &times;${sk.length}<br>${sign}$${sk.pnl.toFixed(2)}<br>#${sk.start+1} &ndash; #${sk.end+1}</div></div>`;
  }).join('');

  const notable = streaks.filter(s => s.length >= 2).sort((a,b) => b.length - a.length);
  document.getElementById('streakBody').innerHTML = notable.length
    ? notable.map(sk => {
        const sign = sk.pnl >= 0 ? '+' : '';
        return `<tr>
          <td><span class="badge ${sk.type==='win'?'badge-win':'badge-loss'}">${sk.type.toUpperCase()}</span></td>
          <td>${sk.length} in a row</td>
          <td>#${sk.start+1}</td>
          <td>#${sk.end+1}</td>
          <td style="color:${sk.pnl>=0?'var(--green)':'var(--red)'}">${sign}$${sk.pnl.toFixed(2)}</td>
          <td style="font-size:.7rem">${fmtTs(trades[sk.start]?.timestamp)}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="6" style="color:var(--text2);text-align:center;padding:16px">No streaks of 2+ yet</td></tr>';
}

// ─── SYMBOL BREAKDOWN ────────────────────────────────────────────────────────
function renderSymbolBreakdown(trades) {
  const map = {};
  for (const t of trades) {
    if (!map[t.symbol]) map[t.symbol] = { wins:0, losses:0, pnl:0, stakes:[] };
    const s = map[t.symbol];
    if (t.result === 'win') s.wins++; else s.losses++;
    s.pnl += t.profit;
    s.stakes.push(t.stake);
  }
  const symbols = Object.entries(map).sort((a,b) => (b[1].wins+b[1].losses)-(a[1].wins+a[1].losses));
  document.getElementById('symbolGrid').innerHTML = symbols.map(([sym, s]) => {
    const total = s.wins + s.losses;
    const wr = total ? (s.wins / total * 100) : 0;
    const pClass = s.pnl >= 0 ? 'c-green' : 'c-red';
    const sign = s.pnl >= 0 ? '+' : '';
    return `<div class="sym-card">
      <div class="sym-name">${sym}</div>
      <div class="sym-stats">
        <div class="sym-stat"><span>Trades</span><span>${total}</span></div>
        <div class="sym-stat"><span>W / L</span><span>${s.wins} / ${s.losses}</span></div>
        <div class="sym-stat"><span>Win Rate</span><span>${wr.toFixed(0)}%</span></div>
        <div class="sym-stat"><span>P&amp;L</span><span class="${pClass}">${sign}$${s.pnl.toFixed(2)}</span></div>
      </div>
      <div class="sym-bar-wrap"><div class="sym-bar-fill" style="width:${wr}%;background:${wr>=50?'var(--green)':'var(--red)'}"></div></div>
    </div>`;
  }).join('');
}

// ─── UTILS ───────────────────────────────────────────────────────────────────
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' })
       + ' ' + d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
}

function computeDuration(start, end) {
  if (!start) return '—';
  const diff = Math.floor(((end ? end : Date.now()/1000) - start));
  if (diff < 60) return diff + 's';
  if (diff < 3600) return Math.floor(diff/60) + 'm ' + (diff%60) + 's';
  return Math.floor(diff/3600) + 'h ' + Math.floor((diff%3600)/60) + 'm';
}

// ─── BOOT ────────────────────────────────────────────────────────────────────
init();
</script>
</body>
</html>
