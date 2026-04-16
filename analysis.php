<?php
/**
 * Deriv Bot Session Dashboard + Bot Control
 *
 * Place in project root (same level as data/ and bot.py)
 * Run with: php -S 0.0.0.0:8080 analysis.php
 *
 * API endpoints:
 *   ?api=sessions              → list all session JSONs
 *   ?api=session&file=X        → return one session JSON
 *   ?api=bot_status            → tmux bbot status + last logs
 *   ?api=bot_start  (POST)     → kill bbot if running, start new bot
 *   ?api=bot_stop   (POST)     → graceful stop + kill bbot
 */

$DATA_DIR  = __DIR__ . '/data';
$BOT_DIR   = __DIR__;
$TMUX_NAME = 'bbot';

// ── API ──────────────────────────────────────────────────────────────────────
if (isset($_GET['api'])) {
    header('Content-Type: application/json');
    header('Cache-Control: no-cache');

    // ── sessions ─────────────────────────────────────────────────────────────
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
                'profit_target'  => $sess['profit_target'] ?? null,
                'loss_limit'     => $sess['loss_limit'] ?? null,
                'trade_count'    => $sum['trade_count'] ?? 0,
                'wins'           => $sum['wins'] ?? 0,
                'losses'         => $sum['losses'] ?? 0,
                'net_pnl'        => $sum['net_pnl'] ?? 0,
                'win_rate'       => $sum['win_rate'] ?? 0,
                'is_live'        => ($sess['active_contract'] !== null),
            ];
        }
        usort($sessions, fn($a,$b) => ($b['started_at']??0) <=> ($a['started_at']??0));
        echo json_encode($sessions);
        exit;
    }

    // ── single session ────────────────────────────────────────────────────────
    if ($_GET['api'] === 'session' && isset($_GET['file'])) {
        $file = basename($_GET['file']);
        $path = $DATA_DIR . '/' . $file;
        if (!file_exists($path)) { http_response_code(404); echo json_encode(['error'=>'Not found']); exit; }
        echo file_get_contents($path);
        exit;
    }

    // ── bot status ────────────────────────────────────────────────────────────
    if ($_GET['api'] === 'bot_status') {
        $out = []; $code = -1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $out, $code);
        $running = $code === 0;
        $logs = '';
        if ($running) {
            $logOut = [];
            exec("tmux capture-pane -t " . escapeshellarg($TMUX_NAME) . " -p -S -50 2>&1", $logOut);
            $logs = implode("\n", $logOut);
        }
        echo json_encode(['running'=>$running,'tmux'=>$TMUX_NAME,'logs'=>$logs]);
        exit;
    }

    // ── bot start (POST) ──────────────────────────────────────────────────────
    if ($_GET['api'] === 'bot_start' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];

        // Always kill existing bbot first
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $chk, $chkCode);
        if ($chkCode === 0) {
            exec("tmux send-keys -t " . escapeshellarg($TMUX_NAME) . " C-c 2>&1");
            usleep(600000);
            exec("tmux kill-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1");
            sleep(1);
        }

        // Build python command
        $default_token = 'gY5gbEpJVhih5NL';
        $token     = (!empty($body['token'])) ? $body['token'] : $default_token;
        $mode      = $body['mode']       === 'real' ? 'real' : 'demo';
        $stake     = floatval($body['base_stake']   ?? 0.35);
        $martingale= floatval($body['martingale']   ?? 2.2);
        $threshold = floatval($body['threshold']    ?? 0.60);
        $strategy  = in_array($body['strategy']??'', ['alphabloom','pulse','ensemble'])
                     ? $body['strategy'] : 'alphabloom';
        $abWindow  = intval($body['ab_window'] ?? 60);
        $disKelly  = !empty($body['disable_kelly']);
        $disRisk   = !empty($body['disable_risk']);
        $profitTgt = isset($body['profit_target']) && $body['profit_target'] !== '' && $body['profit_target'] !== null
                     ? floatval($body['profit_target']) : null;
        $lossLim   = isset($body['loss_limit']) && $body['loss_limit'] !== '' && $body['loss_limit'] !== null
                     ? floatval($body['loss_limit']) : null;

        $cmd = sprintf(
            "python3 bot.py --token %s --account-mode %s --base-stake %.2f --martingale %.2f --score-threshold %.2f --strategy %s",
            escapeshellarg($token), $mode, $stake, $martingale, $threshold, $strategy
        );
        if ($strategy === 'alphabloom') $cmd .= " --ab-window $abWindow";
        if ($disKelly)  $cmd .= " --disable-kelly";
        if ($disRisk)   $cmd .= " --disable-risk-engine";
        if ($profitTgt !== null) $cmd .= sprintf(" --profit-target %.2f", $profitTgt);
        if ($lossLim   !== null) $cmd .= sprintf(" --loss-limit %.2f", $lossLim);

        // Launcher script
        $launcher = $BOT_DIR . '/.launcher.sh';
        $script  = "#!/bin/bash\n";
        $script .= "cd " . escapeshellarg($BOT_DIR) . "\n";
        $script .= "echo \"Starting: $cmd\"\n";
        $script .= "echo \"---\"\n";
        $script .= "$cmd\n";
        $script .= "EXIT=\$?\n";
        $script .= "echo \"\"\necho \"=== Bot exited (code \$EXIT) — press Enter to close ===\"\n";
        $script .= "read\n";
        file_put_contents($launcher, $script);
        chmod($launcher, 0755);

        $tmuxCmd = "tmux new-session -d -s " . escapeshellarg($TMUX_NAME) . " bash " . escapeshellarg($launcher) . " 2>&1";
        $tmuxOut = []; $tmuxRet = -1;
        exec($tmuxCmd, $tmuxOut, $tmuxRet);

        sleep(2);
        $v=[]; $vc=-1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $v, $vc);

        echo json_encode([
            'success'   => ($vc === 0),
            'command'   => $cmd,
            'tmux'      => $TMUX_NAME,
            'ret'       => $tmuxRet,
            'out'       => implode("\n", $tmuxOut),
        ]);
        exit;
    }

    // ── bot stop (POST) ───────────────────────────────────────────────────────
    if ($_GET['api'] === 'bot_stop' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        exec("tmux send-keys -t " . escapeshellarg($TMUX_NAME) . " C-c 2>&1");
        usleep(1500000);
        exec("tmux kill-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1");
        echo json_encode(['success'=>true]);
        exit;
    }

    http_response_code(400);
    echo json_encode(['error'=>'Unknown endpoint']);
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
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--border)}
.header h1{font-size:1.7rem;font-weight:800;letter-spacing:-.02em;background:linear-gradient(135deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .subtitle{color:var(--text2);font-size:.85rem;margin-top:3px}
.header-actions{display:flex;gap:10px;align-items:center}
.auto-refresh-label{color:var(--text2);font-size:.78rem;display:flex;align-items:center;gap:6px;cursor:pointer}
.auto-refresh-label input{accent-color:var(--green)}

/* ── TABS ── */
.tab-bar{display:flex;gap:0;margin-bottom:24px;border-bottom:2px solid var(--border)}
.tab-btn{font-family:var(--font);font-size:.88rem;font-weight:600;padding:12px 24px;border:none;background:none;color:var(--text2);cursor:pointer;position:relative;transition:color .15s;display:flex;align-items:center;gap:8px}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--green)}
.tab-btn.active::after{content:'';position:absolute;bottom:-2px;left:0;right:0;height:2px;background:var(--green)}
.tab-dot{width:7px;height:7px;border-radius:50%}
.tab-dot.on{background:var(--green);box-shadow:0 0 6px var(--green);animation:pulse-live 2s infinite}
.tab-dot.off{background:var(--text3)}
.tab-content{display:none}
.tab-content.active{display:block}
@keyframes pulse-live{0%,100%{opacity:1;box-shadow:0 0 0 0 #00e67660}50%{opacity:.7;box-shadow:0 0 0 6px #00e67600}}

/* ── BUTTONS ── */
.btn{font-family:var(--font);font-size:.82rem;font-weight:600;padding:8px 18px;border-radius:var(--radius-sm);border:none;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none!important}
.btn-primary{background:var(--green);color:#0a0b0f}
.btn-primary:hover:not(:disabled){background:#00ff88;transform:translateY(-1px);box-shadow:0 4px 20px #00e67640}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover:not(:disabled){background:#ff6b6b;transform:translateY(-1px);box-shadow:0 4px 20px #ff525240}
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

/* ── BOT CONTROL ── */
.control-wrap{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
@media(max-width:900px){.control-wrap{grid-template-columns:1fr}}

.ctrl-box{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:22px}
.ctrl-box h3{font-size:.85rem;font-weight:700;margin-bottom:18px;color:var(--text)}

/* status */
.status-row{display:flex;align-items:center;gap:14px;margin-bottom:16px}
.status-dot{width:14px;height:14px;border-radius:50%;flex-shrink:0;transition:background .3s,box-shadow .3s}
.status-dot.on{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse-live 2s infinite}
.status-dot.off{background:var(--red);box-shadow:0 0 6px var(--red)}
.status-label{font-size:1rem;font-weight:700}
.status-sub{font-size:.75rem;color:var(--text2);margin-top:2px}
.ctrl-actions{display:flex;gap:10px;margin-top:4px}

/* form */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group label{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);font-weight:600}
.form-group input,.form-group select{
  font-family:var(--mono);font-size:.88rem;
  padding:9px 12px;border-radius:var(--radius-sm);
  border:1px solid var(--border);background:var(--bg3);color:var(--text);
  outline:none;transition:border-color .15s;width:100%;
}
.form-group input:focus,.form-group select:focus{border-color:var(--green)}
.form-group .hint{font-size:.67rem;color:var(--text3);margin-top:1px}
.form-full{grid-column:1/-1}

/* toggle switch */
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.toggle-row:last-child{border-bottom:none}
.toggle-label{font-size:.8rem;font-weight:600;color:var(--text)}
.toggle-sub{font-size:.7rem;color:var(--text2);margin-top:2px}
.toggle{position:relative;width:42px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:var(--bg4);border:1px solid var(--border2);border-radius:12px;cursor:pointer;transition:.2s}
.toggle-slider::before{content:'';position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:var(--text2);border-radius:50%;transition:.2s}
.toggle input:checked + .toggle-slider{background:var(--green2);border-color:var(--green)}
.toggle input:checked + .toggle-slider::before{transform:translateX(18px);background:var(--green)}

/* mode toggle buttons */
.mode-group{display:flex;gap:0;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden}
.mode-btn{font-family:var(--font);font-size:.8rem;font-weight:700;padding:8px 20px;border:none;background:var(--bg3);color:var(--text2);cursor:pointer;transition:all .15s;flex:1;text-align:center}
.mode-btn.active-demo{background:var(--blue2);color:var(--blue)}
.mode-btn.active-real{background:var(--green2);color:var(--green)}

/* cmd preview */
.cmd-preview{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px 14px;font-family:var(--mono);font-size:.72rem;color:var(--text2);word-break:break-all;line-height:1.6;margin-top:14px;max-height:90px;overflow-y:auto}
.cmd-preview span{color:var(--green)}

/* logs */
.log-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:18px}
.log-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.log-header h3{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text2)}
.log-output{background:#000;border-radius:var(--radius-sm);padding:14px;font-family:var(--mono);font-size:.72rem;color:var(--green);line-height:1.65;max-height:420px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.log-output.empty{color:var(--text3);font-style:italic}

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
  .config-bar,.form-grid{gap:12px}
  .header{flex-direction:column;align-items:flex-start;gap:12px}
  .form-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div class="loading-overlay" id="loader">
  <div class="spinner" style="width:32px;height:32px;border-width:3px"></div>
  <p>Loading...</p>
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
      <button class="btn btn-ghost" onclick="refreshAll()">&#8635; Refresh</button>
    </div>
  </div>

  <!-- TABS -->
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="analytics" onclick="switchTab('analytics')">
      &#128202; Analytics
    </button>
    <button class="tab-btn" data-tab="control" onclick="switchTab('control')">
      &#129302; Bot Control <span class="tab-dot off" id="tabDot"></span>
    </button>
  </div>

  <!-- ════════════════════════ TAB: ANALYTICS ════════════════════════ -->
  <div class="tab-content active" id="tab-analytics">

    <div class="sessions-panel">
      <h2>Sessions <span id="sessionCount" style="color:var(--text3);font-weight:400"></span></h2>
      <div class="session-grid" id="sessionGrid"></div>
    </div>

    <div class="empty-state" id="emptyState">
      <div class="big-icon">&#128202;</div>
      <p>Select a session above to view detailed stats, trade history, equity curve, and symbol breakdown.</p>
    </div>

    <div class="dashboard" id="dashboard">
      <div class="config-bar" id="configBar"></div>
      <div class="stats-row" id="statsRow"></div>
      <div class="chart-row">
        <div class="chart-section"><h3>Equity Curve</h3><div class="chart-container"><canvas id="equityChart"></canvas></div></div>
        <div class="chart-section"><h3>Cumulative P&amp;L</h3><div class="chart-container"><canvas id="pnlChart"></canvas></div></div>
      </div>
      <div class="chart-row">
        <div class="chart-section"><h3>Stake Progression</h3><div class="chart-container"><canvas id="stakeChart"></canvas></div></div>
        <div class="chart-section"><h3>Win / Loss Distribution</h3><div class="chart-container"><canvas id="wlChart"></canvas></div></div>
      </div>
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
      <div class="symbol-section">
        <h3 style="font-size:.73rem;text-transform:uppercase;letter-spacing:.08em;color:var(--text2);margin-bottom:10px;font-weight:600">Symbol Breakdown</h3>
        <div class="symbol-grid" id="symbolGrid"></div>
      </div>
      <div class="trade-log">
        <div class="trade-log-header">
          <h3>Trade Log</h3>
          <span class="trade-count" id="tradeCount"></span>
        </div>
        <div class="trade-table-wrap">
          <table class="trade-table">
            <thead><tr><th>#</th><th>Time</th><th>Symbol</th><th>Type</th><th>Result</th><th>Stake</th><th>Profit</th><th>Payout</th><th>Cum. P&amp;L</th><th>Equity</th></tr></thead>
            <tbody id="tradeBody"></tbody>
          </table>
        </div>
      </div>
    </div>

  </div><!-- /tab-analytics -->

  <!-- ════════════════════════ TAB: BOT CONTROL ════════════════════════ -->
  <div class="tab-content" id="tab-control">

    <div class="control-wrap">

      <!-- LEFT: Status + Form -->
      <div>

        <!-- Status card -->
        <div class="ctrl-box" style="margin-bottom:16px">
          <div class="status-row">
            <div class="status-dot off" id="statusDot"></div>
            <div>
              <div class="status-label" id="statusLabel">Checking...</div>
              <div class="status-sub">tmux session: <code>bbot</code></div>
            </div>
          </div>
          <div class="ctrl-actions">
            <button class="btn btn-danger" id="stopBtn" onclick="stopBot()" disabled>&#9209; Stop Bot</button>
          </div>
        </div>

        <!-- Config form -->
        <div class="ctrl-box">
          <h3>&#9881; Configure &amp; Start</h3>

          <!-- Mode toggle -->
          <div style="margin-bottom:16px">
            <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);font-weight:600;margin-bottom:8px">Account Mode</div>
            <div class="mode-group">
              <button class="mode-btn active-demo" id="modeDemo" onclick="setMode('demo')">Demo</button>
              <button class="mode-btn" id="modeReal" onclick="setMode('real')">Real</button>
            </div>
          </div>

          <div class="form-grid">
            <div class="form-group form-full">
              <label>API Token</label>
              <input type="text" id="fToken" value="" placeholder="Leave empty to use default token" oninput="updateCmd()">
              <span class="hint">Token must match the selected account mode</span>
            </div>
            <div class="form-group">
              <label>Base Stake (USD)</label>
              <input type="number" id="fStake" value="0.35" step="0.01" min="0.35" oninput="updateCmd()">
              <span class="hint">Min $0.35 (Deriv minimum)</span>
            </div>
            <div class="form-group">
              <label>Martingale Multiplier</label>
              <input type="number" id="fMartingale" value="2.2" step="0.1" min="1" oninput="updateCmd()">
              <span class="hint">Multiply stake on each loss</span>
            </div>
            <div class="form-group">
              <label>Score Threshold</label>
              <input type="number" id="fThreshold" value="0.60" step="0.01" min="0" max="1" oninput="updateCmd()">
              <span class="hint">Min signal confidence (0–1)</span>
            </div>
            <div class="form-group">
              <label>Strategy</label>
              <select id="fStrategy" onchange="onStrategyChange(); updateCmd()">
                <option value="alphabloom" selected>AlphaBloom</option>
                <option value="pulse">Pulse (dual-timeframe)</option>
                <option value="ensemble">Ensemble</option>
              </select>
            </div>
            <div class="form-group" id="abWindowGroup">
              <label>AB Window (ticks)</label>
              <input type="number" id="fAbWindow" value="60" step="5" min="10" oninput="updateCmd()">
              <span class="hint">AlphaBloom analysis window</span>
            </div>
            <div class="form-group">
              <label>Take Profit (USD) <span style="color:var(--text3)">optional</span></label>
              <input type="number" id="fProfit" value="" step="1" min="0" placeholder="e.g. 50" oninput="updateCmd()">
              <span class="hint">Leave empty to run forever</span>
            </div>
            <div class="form-group">
              <label>Loss Limit (USD) <span style="color:var(--text3)">optional</span></label>
              <input type="number" id="fLoss" value="" step="1" placeholder="e.g. -30" oninput="updateCmd()">
              <span class="hint">Negative value, e.g. -30</span>
            </div>
          </div>

          <!-- Toggles -->
          <div style="margin-top:16px;background:var(--bg3);border-radius:var(--radius-sm);padding:4px 14px">
            <div class="toggle-row">
              <div>
                <div class="toggle-label">Disable Kelly Sizing</div>
                <div class="toggle-sub">Use base stake + martingale only</div>
              </div>
              <label class="toggle"><input type="checkbox" id="tKelly" checked onchange="updateCmd()"><span class="toggle-slider"></span></label>
            </div>
            <div class="toggle-row">
              <div>
                <div class="toggle-label">Disable Risk Engine</div>
                <div class="toggle-sub">No cooldown, no circuit breaker</div>
              </div>
              <label class="toggle"><input type="checkbox" id="tRisk" onchange="updateCmd()"><span class="toggle-slider"></span></label>
            </div>
          </div>

          <!-- Command preview -->
          <div class="cmd-preview" id="cmdPreview"></div>

          <div style="margin-top:16px;display:flex;gap:10px">
            <button class="btn btn-primary" id="startBtn" onclick="startBot()" style="flex:1">&#128640; Start Bot</button>
          </div>
        </div>

      </div><!-- /left -->

      <!-- RIGHT: Live Logs -->
      <div class="log-wrap">
        <div class="log-header">
          <h3>Live Logs — bbot</h3>
          <button class="btn btn-ghost" onclick="refreshBotStatus()" style="padding:5px 12px;font-size:.75rem">&#8635; Refresh</button>
        </div>
        <div class="log-output empty" id="logOutput">Bot is not running.</div>
      </div>

    </div><!-- /control-wrap -->
  </div><!-- /tab-control -->

</div><!-- /container -->

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
// ─── STATE ───────────────────────────────────────────────────────────────────
let sessions = [], activeFile = null, activeData = null;
let charts = {}, autoTimer = null, botRunning = false;
let currentMode = 'demo';

// ─── TABS ─────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tab));
  if (tab === 'control') refreshBotStatus();
}

// ─── API ─────────────────────────────────────────────────────────────────────
async function apiFetch(url, opts) {
  const r = await fetch(url, { cache:'no-store', ...opts });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
async function apiPost(url, body) {
  return apiFetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
async function init() {
  updateCmd();
  await loadSessions();
  document.getElementById('loader').classList.add('hidden');
}

async function loadSessions() {
  try {
    sessions = await apiFetch('?api=sessions');
    renderSessionGrid();
    if (activeFile) {
      if (sessions.find(s => s.file === activeFile)) await loadSession(activeFile, false);
    }
  } catch(e) { console.error('Sessions:', e); }
}

function refreshAll() {
  loadSessions();
  if (document.getElementById('tab-control').classList.contains('active')) refreshBotStatus();
}

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
    grid.innerHTML = '<div style="color:var(--text2);padding:20px;font-size:.85rem">No JSON files found in data/ folder.</div>';
    return;
  }
  grid.innerHTML = sessions.map(s => {
    const pnl = s.net_pnl ?? 0;
    const pnlClass = pnl >= 0 ? 'c-green' : 'c-red';
    const wr = ((s.win_rate ?? 0) * 100).toFixed(1);
    const modeBadge = s.account_mode === 'live'
      ? '<span class="sc-badge live-badge">Live</span>'
      : '<span class="sc-badge demo">Demo</span>';
    return `
    <div class="session-card ${s.is_live?'live-card':''} ${activeFile===s.file?'active-card':''}"
         data-file="${s.file}" onclick="loadSession('${s.file}')">
      <div class="sc-top"><div class="sc-id">${s.file.replace('.json','')}</div>${modeBadge}</div>
      <div class="sc-date">${fmtTs(s.started_at)}${s.is_live?' &bull; <span style="color:var(--green)">LIVE</span>':''}</div>
      <div class="sc-stats">
        <div class="sc-stat"><span>Trades</span><span>${s.trade_count}</span></div>
        <div class="sc-stat"><span>Win Rate</span><span>${wr}%</span></div>
        <div class="sc-stat"><span>W / L</span><span>${s.wins} / ${s.losses}</span></div>
        <div class="sc-stat"><span>P&amp;L</span><span class="${pnlClass}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span></div>
      </div>
    </div>`;
  }).join('');
}

// ─── LOAD SESSION ─────────────────────────────────────────────────────────────
async function loadSession(file, scroll=true) {
  activeFile = file;
  try {
    activeData = await apiFetch('?api=session&file=' + encodeURIComponent(file));
    renderDashboard();
    document.getElementById('emptyState').style.display = 'none';
    document.getElementById('dashboard').classList.add('visible');
    document.querySelectorAll('.session-card').forEach(c =>
      c.classList.toggle('active-card', c.dataset.file === file));
    if (scroll) document.getElementById('dashboard').scrollIntoView({ behavior:'smooth', block:'start' });
  } catch(e) { console.error('Session load:', e); }
}

// ─── RENDER DASHBOARD ─────────────────────────────────────────────────────────
function renderDashboard() {
  if (!activeData) return;
  const d = activeData, sess = d.session||{}, sum = d.summary||{};
  const trades = d.trades||[], curve = d.equity_curve||[];

  document.getElementById('configBar').innerHTML = `
    <div class="config-item"><span class="clabel">Account</span><span class="cval">${sess.account_loginid||'—'}</span></div>
    <div class="config-item"><span class="clabel">Name</span><span class="cval" style="font-size:.78rem">${sess.account_fullname?.trim()||'—'}</span></div>
    <div class="config-item"><span class="clabel">Mode</span><span class="cval" style="color:${sess.account_mode==='live'?'var(--green)':'var(--blue)'};text-transform:capitalize">${sess.account_mode||'demo'}</span></div>
    <div class="config-item"><span class="clabel">Base Stake</span><span class="cval">$${sess.base_stake??'—'}</span></div>
    <div class="config-item"><span class="clabel">Profit Target</span><span class="cval" style="color:var(--green)">${sess.profit_target!=null?'$'+sess.profit_target:'unlimited'}</span></div>
    <div class="config-item"><span class="clabel">Loss Limit</span><span class="cval" style="color:var(--red)">${sess.loss_limit!=null?'$'+sess.loss_limit:'unlimited'}</span></div>
    <div class="config-item"><span class="clabel">Score Thr.</span><span class="cval">${sess.score_threshold??'—'}</span></div>
    <div class="config-item"><span class="clabel">Duration</span><span class="cval">${sess.duration??'—'} ${sess.duration_unit||''}</span></div>
    <div class="config-item"><span class="clabel">Started</span><span class="cval" style="font-size:.75rem">${fmtTs(sess.started_at)}</span></div>
    <div class="config-item"><span class="clabel">Symbols</span><span class="cval"><div class="config-symbols">${(sess.symbols||[]).map(s=>`<span class="sym-tag">${s}</span>`).join('')}</div></span></div>
  `;

  const initEq = sess.initial_equity||0, curEq = sess.current_equity||0;
  const eqChange = curEq - initEq, pnl = sum.net_pnl??0, wr = (sum.win_rate??0)*100;

  let runPeak = initEq, maxDD = 0;
  for (const pt of curve) {
    if (pt.equity > runPeak) runPeak = pt.equity;
    const dd = runPeak - pt.equity;
    if (dd > maxDD) maxDD = dd;
  }

  const winT = trades.filter(t=>t.result==='win'), lossT = trades.filter(t=>t.result==='loss');
  const avgW = winT.length ? winT.reduce((a,t)=>a+t.profit,0)/winT.length : 0;
  const avgL = lossT.length ? lossT.reduce((a,t)=>a+Math.abs(t.profit),0)/lossT.length : 0;
  const streaks = computeStreaks(trades);
  const maxWS = Math.max(0,...streaks.filter(s=>s.type==='win').map(s=>s.length));
  const maxLS = Math.max(0,...streaks.filter(s=>s.type==='loss').map(s=>s.length));
  const dur = computeDuration(sess.started_at, sess.updated_at);
  const pc = pnl>=0?'c-green':'c-red', ec = eqChange>=0?'c-green':'c-red';

  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card ${pnl>=0?'hl-green':'hl-red'}">
      <div class="label">Net P&amp;L</div><div class="value ${pc}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div><div class="sub">${sum.trade_count||0} trades</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value">${wr.toFixed(1)}%</div><div class="sub">${sum.wins||0}W / ${sum.losses||0}L</div></div>
    <div class="stat-card"><div class="label">Equity</div><div class="value">$${curEq.toFixed(2)}</div><div class="sub ${ec}">${eqChange>=0?'+':''}$${eqChange.toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">Peak Equity</div><div class="value c-green">$${runPeak.toFixed(2)}</div><div class="sub">+$${(runPeak-initEq).toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">Max Drawdown</div><div class="value c-red">-$${maxDD.toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">Avg Win / Loss</div><div class="value" style="font-size:1rem"><span class="c-green">$${avgW.toFixed(2)}</span><span style="color:var(--text3)"> / </span><span class="c-red">$${avgL.toFixed(2)}</span></div></div>
    <div class="stat-card"><div class="label">Best/Worst Streak</div><div class="value" style="font-size:1rem"><span class="c-green">${maxWS}W</span><span style="color:var(--text3)"> / </span><span class="c-red">${maxLS}L</span></div></div>
    <div class="stat-card"><div class="label">Duration</div><div class="value" style="font-size:1.05rem">${dur}</div><div class="sub">thr: ${sess.score_threshold??'—'}</div></div>
  `;

  renderCharts(trades, curve);
  renderStreaks(streaks, trades);
  renderSymbolBreakdown(trades);

  document.getElementById('tradeCount').textContent = `${trades.length} trades`;
  let cum = 0;
  document.getElementById('tradeBody').innerHTML = trades.map(t => {
    cum += t.profit;
    const win = t.result==='win', pc2 = win?'c-green':'c-red', ps = t.profit>=0?'+':'', cs = cum>=0?'+':'';
    const typeBadge = t.contract_type.includes('ODD') ? '<span class="badge badge-odd">ODD</span>' : '<span class="badge badge-even">EVEN</span>';
    return `<tr>
      <td>${t.trade_no}</td>
      <td style="font-size:.7rem">${fmtTs(t.timestamp)}</td>
      <td><span class="sym-tag" style="font-size:.68rem">${t.symbol}</span></td>
      <td>${typeBadge}</td>
      <td><span class="badge ${win?'badge-win':'badge-loss'}">${t.result.toUpperCase()}</span></td>
      <td>$${t.stake.toFixed(2)}</td>
      <td class="${pc2}">${ps}$${t.profit.toFixed(2)}</td>
      <td>$${t.payout.toFixed(2)}</td>
      <td style="color:${cum>=0?'var(--green)':'var(--red)'}">${cs}$${cum.toFixed(2)}</td>
      <td>$${t.equity_after.toFixed(2)}</td>
    </tr>`;
  }).join('');
}

// ─── CHARTS ──────────────────────────────────────────────────────────────────
function destroyCharts(){Object.values(charts).forEach(c=>c.destroy());charts={}}
const TICK='#9395aa', FM='JetBrains Mono';
function bOpts(){return{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{titleColor:'#e2e4f0',bodyColor:'#e2e4f0',backgroundColor:'#1a1b25ee',borderColor:'#3a3b5d',borderWidth:1,titleFont:{family:FM,size:11},bodyFont:{family:FM,size:11}}},scales:{x:{grid:{color:'#2a2b3d40'},ticks:{color:TICK,font:{family:FM,size:10}}},y:{grid:{color:'#2a2b3d40'},ticks:{color:TICK,font:{family:FM,size:10}}}}}}
function dTicks(o){o.scales.y.ticks.callback=v=>'$'+v.toFixed(2);return o}

function renderCharts(trades, curve) {
  destroyCharts();
  if (!trades.length) return;
  const labels = trades.map(t=>'#'+t.trade_no);
  const ptC = trades.map(t=>t.result==='win'?'#00e676':'#ff5252');

  const eqCtx = document.getElementById('equityChart').getContext('2d');
  const eqG = eqCtx.createLinearGradient(0,0,0,280); eqG.addColorStop(0,'#18ffff30'); eqG.addColorStop(1,'#18ffff00');
  charts.eq = new Chart(eqCtx,{type:'line',data:{labels:curve.map(p=>'#'+p.trade_no),datasets:[{data:curve.map(p=>p.equity),borderColor:'#18ffff',backgroundColor:eqG,fill:true,tension:.3,pointRadius:curve.length>60?0:3,borderWidth:2}]},options:dTicks({...bOpts()})});

  let cum=0; const pnlV = trades.map(t=>{cum+=t.profit;return cum;});
  const fp = pnlV[pnlV.length-1];
  const pCtx = document.getElementById('pnlChart').getContext('2d');
  const pG = pCtx.createLinearGradient(0,0,0,280);
  if(fp>=0){pG.addColorStop(0,'#00e67640');pG.addColorStop(1,'#00e67600')}else{pG.addColorStop(0,'#ff525200');pG.addColorStop(1,'#ff525230')}
  charts.pnl = new Chart(pCtx,{type:'line',data:{labels,datasets:[{data:pnlV,borderColor:fp>=0?'#00e676':'#ff5252',backgroundColor:pG,fill:true,tension:.3,pointRadius:trades.length>60?0:3,pointBackgroundColor:ptC,borderWidth:2}]},options:dTicks({...bOpts()})});

  charts.stake = new Chart(document.getElementById('stakeChart').getContext('2d'),{type:'bar',data:{labels,datasets:[{data:trades.map(t=>t.stake),backgroundColor:ptC.map(c=>c+'80'),borderColor:ptC,borderWidth:1,borderRadius:3}]},options:dTicks(bOpts())});

  const wins=trades.filter(t=>t.result==='win').length;
  charts.wl = new Chart(document.getElementById('wlChart').getContext('2d'),{type:'doughnut',data:{labels:['Wins','Losses'],datasets:[{data:[wins,trades.length-wins],backgroundColor:['#00e67690','#ff525290'],borderColor:['#00e676','#ff5252'],borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{position:'bottom',labels:{color:TICK,font:{family:'Outfit',size:12},padding:16}}}}});
}

// ─── STREAKS ─────────────────────────────────────────────────────────────────
function computeStreaks(trades) {
  if (!trades.length) return [];
  const out=[]; let cur={type:trades[0].result,start:0,length:1,pnl:trades[0].profit};
  for(let i=1;i<trades.length;i++){
    if(trades[i].result===cur.type){cur.length++;cur.pnl+=trades[i].profit}
    else{cur.end=i-1;out.push({...cur});cur={type:trades[i].result,start:i,length:1,pnl:trades[i].profit}}
  }
  cur.end=trades.length-1;out.push(cur);return out;
}
function renderStreaks(streaks,trades){
  const total=trades.length||1;
  document.getElementById('streakBar').innerHTML=streaks.map(sk=>{
    const pct=(sk.length/total)*100,sign=sk.pnl>=0?'+':'';
    return `<div class="streak-seg ${sk.type}" style="width:${Math.max(pct,1.5)}%">${sk.length>2?sk.length:''}<div class="streak-tip">${sk.type.toUpperCase()} &times;${sk.length}<br>${sign}$${sk.pnl.toFixed(2)}<br>#${sk.start+1}&ndash;#${sk.end+1}</div></div>`;
  }).join('');
  const notable=streaks.filter(s=>s.length>=2).sort((a,b)=>b.length-a.length);
  document.getElementById('streakBody').innerHTML=notable.length
    ?notable.map(sk=>{const sign=sk.pnl>=0?'+':'';return`<tr><td><span class="badge ${sk.type==='win'?'badge-win':'badge-loss'}">${sk.type.toUpperCase()}</span></td><td>${sk.length} in a row</td><td>#${sk.start+1}</td><td>#${sk.end+1}</td><td style="color:${sk.pnl>=0?'var(--green)':'var(--red)'}">${sign}$${sk.pnl.toFixed(2)}</td><td style="font-size:.7rem">${fmtTs(trades[sk.start]?.timestamp)}</td></tr>`;}).join('')
    :'<tr><td colspan="6" style="color:var(--text2);text-align:center;padding:16px">No streaks of 2+ yet</td></tr>';
}

// ─── SYMBOL BREAKDOWN ─────────────────────────────────────────────────────────
function renderSymbolBreakdown(trades){
  const map={};
  for(const t of trades){
    if(!map[t.symbol])map[t.symbol]={wins:0,losses:0,pnl:0};
    if(t.result==='win')map[t.symbol].wins++;else map[t.symbol].losses++;
    map[t.symbol].pnl+=t.profit;
  }
  document.getElementById('symbolGrid').innerHTML=Object.entries(map).sort((a,b)=>(b[1].wins+b[1].losses)-(a[1].wins+a[1].losses)).map(([sym,s])=>{
    const tot=s.wins+s.losses,wr=tot?(s.wins/tot*100):0,pc=s.pnl>=0?'c-green':'c-red',sign=s.pnl>=0?'+':'';
    return`<div class="sym-card"><div class="sym-name">${sym}</div><div class="sym-stats"><div class="sym-stat"><span>Trades</span><span>${tot}</span></div><div class="sym-stat"><span>W/L</span><span>${s.wins}/${s.losses}</span></div><div class="sym-stat"><span>Win%</span><span>${wr.toFixed(0)}%</span></div><div class="sym-stat"><span>P&amp;L</span><span class="${pc}">${sign}$${s.pnl.toFixed(2)}</span></div></div><div class="sym-bar-wrap"><div class="sym-bar-fill" style="width:${wr}%;background:${wr>=50?'var(--green)':'var(--red)'}"></div></div></div>`;
  }).join('');
}

// ─── BOT CONTROL ─────────────────────────────────────────────────────────────
function setMode(mode) {
  currentMode = mode;
  document.getElementById('modeDemo').className = 'mode-btn' + (mode==='demo'?' active-demo':'');
  document.getElementById('modeReal').className = 'mode-btn' + (mode==='real'?' active-real':'');
  updateCmd();
}

function onStrategyChange() {
  const s = document.getElementById('fStrategy').value;
  document.getElementById('abWindowGroup').style.display = s === 'alphabloom' ? '' : 'none';
}

function buildParams() {
  const token     = document.getElementById('fToken').value.trim() || 'gY5gbEpJVhih5NL';
  const stake     = parseFloat(document.getElementById('fStake').value) || 0.35;
  const mart      = parseFloat(document.getElementById('fMartingale').value) || 2.2;
  const thr       = parseFloat(document.getElementById('fThreshold').value) || 0.60;
  const strategy  = document.getElementById('fStrategy').value;
  const abWindow  = parseInt(document.getElementById('fAbWindow').value) || 60;
  const disKelly  = document.getElementById('tKelly').checked;
  const disRisk   = document.getElementById('tRisk').checked;
  const profitRaw = document.getElementById('fProfit').value.trim();
  const lossRaw   = document.getElementById('fLoss').value.trim();
  const profit    = profitRaw !== '' ? parseFloat(profitRaw) : null;
  const loss      = lossRaw  !== '' ? parseFloat(lossRaw)  : null;
  return { token, mode:currentMode, base_stake:stake, martingale:mart, threshold:thr,
           strategy, ab_window:abWindow, disable_kelly:disKelly, disable_risk:disRisk,
           profit_target:profit, loss_limit:loss };
}

function updateCmd() {
  const p = buildParams();
  const tokenDisplay = p.token ? p.token : '[default]';
  let cmd = `python3 bot.py --token <span>${tokenDisplay}</span> --account-mode ${p.mode}`;
  cmd += ` --base-stake ${p.base_stake.toFixed(2)} --martingale ${p.martingale.toFixed(1)}`;
  cmd += ` --score-threshold ${p.threshold.toFixed(2)} --strategy ${p.strategy}`;
  if (p.strategy === 'alphabloom') cmd += ` --ab-window ${p.ab_window}`;
  if (p.disable_kelly)  cmd += ' --disable-kelly';
  if (p.disable_risk)   cmd += ' --disable-risk-engine';
  if (p.profit_target !== null) cmd += ` --profit-target ${p.profit_target}`;
  if (p.loss_limit    !== null) cmd += ` --loss-limit ${p.loss_limit}`;
  document.getElementById('cmdPreview').innerHTML = cmd;
}

async function refreshBotStatus() {
  try {
    const data = await apiFetch('?api=bot_status');
    botRunning = data.running;
    const dot = document.getElementById('statusDot');
    const lbl = document.getElementById('statusLabel');
    const tabDot = document.getElementById('tabDot');
    const stopBtn = document.getElementById('stopBtn');
    if (botRunning) {
      dot.className = 'status-dot on';
      lbl.textContent = 'Bot is RUNNING';
      lbl.style.color = 'var(--green)';
      tabDot.className = 'tab-dot on';
      stopBtn.disabled = false;
    } else {
      dot.className = 'status-dot off';
      lbl.textContent = 'Bot is STOPPED';
      lbl.style.color = 'var(--red)';
      tabDot.className = 'tab-dot off';
      stopBtn.disabled = true;
    }
    const logEl = document.getElementById('logOutput');
    if (data.logs && data.logs.trim()) {
      logEl.className = 'log-output';
      logEl.textContent = data.logs;
      logEl.scrollTop = logEl.scrollHeight;
    } else {
      logEl.className = 'log-output empty';
      logEl.textContent = botRunning ? 'Waiting for output...' : 'Bot is not running.';
    }
  } catch(e) { console.error('Bot status:', e); }
}

async function startBot() {
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-width:2px"></div> Starting...';
  try {
    const params = buildParams();
    const res = await apiPost('?api=bot_start', params);
    if (res.success) {
      setTimeout(async () => { await refreshBotStatus(); await loadSessions(); }, 2500);
    } else {
      alert('Failed to start bot.\nReturn: ' + res.ret + '\n' + (res.out || ''));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
  btn.disabled = false;
  btn.innerHTML = '&#128640; Start Bot';
}

async function stopBot() {
  if (!confirm('Send Ctrl+C and kill the bbot tmux session?')) return;
  const btn = document.getElementById('stopBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-width:2px"></div> Stopping...';
  try {
    await apiPost('?api=bot_stop', {});
    setTimeout(async () => {
      await refreshBotStatus();
      await loadSessions();
      btn.innerHTML = '&#9209; Stop Bot';
    }, 2500);
  } catch(e) {
    btn.disabled = false;
    btn.innerHTML = '&#9209; Stop Bot';
  }
}

// ─── UTILS ───────────────────────────────────────────────────────────────────
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'})
       + ' ' + d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
}
function computeDuration(start,end) {
  if (!start) return '—';
  const diff = Math.floor(((end?end:Date.now()/1000)-start));
  if (diff<60) return diff+'s';
  if (diff<3600) return Math.floor(diff/60)+'m '+(diff%60)+'s';
  return Math.floor(diff/3600)+'h '+Math.floor((diff%3600)/60)+'m';
}

// ─── BOOT ────────────────────────────────────────────────────────────────────
onStrategyChange();
init();
</script>
</body>
</html>
