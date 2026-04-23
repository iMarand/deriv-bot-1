<?php
/**
 * Deriv Bot Session Dashboard + Bot Control — Redesigned
 * Run with: php -S 0.0.0.0:8080 analysis.php
 */

$DATA_DIR  = __DIR__ . '/data';
$BOT_DIR   = __DIR__;
$TMUX_NAME = 'bbot';

// ── API ──────────────────────────────────────────────────────────────────────
if (isset($_GET['api'])) {
    header('Content-Type: application/json');
    header('Cache-Control: no-cache');

    if ($_GET['api'] === 'sessions') {
        $files = glob($DATA_DIR . '/*.json');
        if (!$files) $files = [];
        usort($files, fn($a, $b) => filemtime($b) <=> filemtime($a));
        
        $offset = isset($_GET['offset']) ? intval($_GET['offset']) : 0;
        $limit = isset($_GET['limit']) ? intval($_GET['limit']) : 10;
        $slice = array_slice($files, $offset, $limit);

        $sessions = [];
        foreach ($slice as $f) {
            $raw = @file_get_contents($f);
            if (!$raw) continue;
            $d = json_decode($raw, true);
            if (!$d) continue;
            // Ignore files that are not sessions
            if (!isset($d['session'])) continue;
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
                'is_live'        => ((time() - filemtime($f)) < 120),
            ];
        }
        usort($sessions, fn($a,$b) => ($b['started_at']??0) <=> ($a['started_at']??0));
        echo json_encode($sessions);
        exit;
    }

    if ($_GET['api'] === 'session' && isset($_GET['file'])) {
        $file = basename($_GET['file']);
        $path = $DATA_DIR . '/' . $file;
        if (!file_exists($path)) { http_response_code(404); echo json_encode(['error'=>'Not found']); exit; }
        echo file_get_contents($path);
        exit;
    }

    if ($_GET['api'] === 'bot_status') {
        $out = []; $code = -1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $out, $code);
        $running = $code === 0;
        $logs = '';
        if ($running) {
            $logOut = [];
            exec("tmux capture-pane -t " . escapeshellarg($TMUX_NAME) . " -p -S -100 2>&1", $logOut);
            $logs = implode("\n", $logOut);
        }
        echo json_encode(['running'=>$running,'tmux'=>$TMUX_NAME,'logs'=>$logs]);
        exit;
    }


    if ($_GET['api'] === 'bot_start' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $chk, $chkCode);
        if ($chkCode === 0) {
            exec("tmux send-keys -t " . escapeshellarg($TMUX_NAME) . " C-c 2>&1");
            usleep(600000);
            exec("tmux kill-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1");
            exec("pkill -9 -f \"bot.py\" 2>&1");
            sleep(1);
        }
        $default_token = 'gY5gbEpJVhih5NL';
        $token     = (!empty($body['token'])) ? $body['token'] : $default_token;
        $mode      = $body['mode'] === 'real' ? 'real' : 'demo';
        $stake     = floatval($body['base_stake']   ?? 0.35);
        $martingale= floatval($body['martingale']   ?? 2.2);
        $maxStake  = floatval($body['max_stake']    ?? 50.0);
        $threshold = floatval($body['threshold']    ?? 0.60);
        $strategy  = in_array($body['strategy']??'', ['alphabloom','pulse','ensemble','adaptive','novaburst'])
                     ? $body['strategy'] : 'alphabloom';
        $tradeStrategy = in_array($body['trade_strategy']??'', ['even_odd','rise_fall_roll','rise_fall_zigzag','higher_lower_roll','higher_lower_zigzag','over_under_roll','touch_notouch_zigzag'])
                         ? $body['trade_strategy'] : 'even_odd';
        $abWindow  = intval($body['ab_window'] ?? 60);
        $disKelly  = !empty($body['disable_kelly']);
        $disRisk   = !empty($body['disable_risk']);
        $mlOn      = !empty($body['ml_filter']);
        $mlThr     = isset($body['ml_threshold']) && $body['ml_threshold'] !== '' && $body['ml_threshold'] !== null
                     ? floatval($body['ml_threshold']) : null;
        $hotCold   = floatval($body['hotness_cold']   ?? 0.43);
        $hotProbe  = intval($body['hotness_probe']    ?? 20);
        $mlIdle    = floatval($body['ml_idle_minutes'] ?? 10.0);
        $mlFloor   = floatval($body['ml_floor']       ?? 0.35);
        $volSkip   = floatval($body['vol_skip_pct']   ?? 0.75);
        $profitTgt = isset($body['profit_target']) && $body['profit_target'] !== '' && $body['profit_target'] !== null
                     ? floatval($body['profit_target']) : null;
        $lossLim   = isset($body['loss_limit']) && $body['loss_limit'] !== '' && $body['loss_limit'] !== null
                     ? floatval($body['loss_limit']) : null;
        $symbolsRaw = $body['symbols'] ?? [];
        $allowedSymbols = ['R_10','R_25','R_50','R_75','R_100','1HZ10V','1HZ25V','1HZ50V','1HZ75V','1HZ100V'];
        $selectedSymbols = [];
        if (is_array($symbolsRaw) && count($symbolsRaw) > 0) {
            foreach ($symbolsRaw as $s) {
                if (in_array($s, $allowedSymbols)) $selectedSymbols[] = $s;
            }
        }
        $cmd = sprintf(
            "python3 bot.py --token %s --account-mode %s --base-stake %.2f --martingale %.2f --max-stake %.2f --score-threshold %.2f --strategy %s --trade-strategy %s",
            escapeshellarg($token), $mode, $stake, $martingale, $maxStake, $threshold, $strategy, $tradeStrategy
        );
        if (count($selectedSymbols) > 0) {
            $cmd .= ' --symbols ' . implode(' ', array_map('escapeshellarg', $selectedSymbols));
        }
        if ($strategy === 'alphabloom') $cmd .= " --ab-window $abWindow";
        if ($disKelly)  $cmd .= " --disable-kelly";
        if ($disRisk)   $cmd .= " --disable-risk-engine";
        if ($mlOn && $strategy !== 'adaptive') $cmd .= " --ml-filter";
        if (($mlOn || $strategy === 'adaptive') && $mlThr !== null) {
            $cmd .= sprintf(" --ml-threshold %.2f", $mlThr);
        }
        if ($strategy === 'adaptive') {
            $cmd .= sprintf(" --hotness-cold %.2f --hotness-probe %d --vol-skip-pct %.2f", $hotCold, $hotProbe, $volSkip);
            $cmd .= sprintf(" --ml-idle-minutes %.1f --ml-floor %.2f", $mlIdle, $mlFloor);
        }
        if ($profitTgt !== null) $cmd .= sprintf(" --profit-target %.2f", $profitTgt);
        if ($lossLim   !== null) $cmd .= sprintf(" --loss-limit %.2f", $lossLim);

        $launcher = $BOT_DIR . '/.launcher.sh';
        $script  = "#!/bin/bash\n";
        $script .= "cd " . escapeshellarg($BOT_DIR) . "\n";
        $script .= "echo \"Starting: $cmd\"\necho \"---\"\n$cmd\n";
        $script .= "EXIT=\$?\necho \"\"\necho \"=== Bot exited (code \$EXIT) — press Enter to close ===\"\nread\n";
        file_put_contents($launcher, $script);
        chmod($launcher, 0755);
        $tmuxCmd = "tmux new-session -d -s " . escapeshellarg($TMUX_NAME) . " bash " . escapeshellarg($launcher) . " 2>&1";
        $tmuxOut = []; $tmuxRet = -1;
        exec($tmuxCmd, $tmuxOut, $tmuxRet);
        sleep(2);
        $v=[]; $vc=-1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $v, $vc);
        echo json_encode(['success'=>($vc===0),'command'=>$cmd,'tmux'=>$TMUX_NAME,'ret'=>$tmuxRet,'out'=>implode("\n",$tmuxOut)]);
        exit;
    }

    if ($_GET['api'] === 'bot_stop' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        exec("tmux send-keys -t " . escapeshellarg($TMUX_NAME) . " C-c 2>&1");
        usleep(1500000);
        exec("tmux kill-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1");
        exec("pkill -9 -f \"bot.py\" 2>&1");
        echo json_encode(['success'=>true]);
        exit;
    }

    if ($_GET['api'] === 'ml_files') {
        $files = glob($DATA_DIR . '/*.json') ?: [];
        $out = [];
        foreach ($files as $f) {
            $name = basename($f);
            if ($name === 'ml_filter.json') continue;
            $raw = @file_get_contents($f);
            if (!$raw) continue;
            $d = json_decode($raw, true);
            if (!is_array($d)) continue;
            $trades = $d['trades'] ?? null;
            if (!is_array($trades) || !count($trades)) continue;
            $labeled = 0;
            foreach ($trades as $t) {
                if (!isset($t['symbol'], $t['contract_type'], $t['result'])) continue;
                $labeled++;
            }
            if (!$labeled) continue;
            $out[] = ['file'=>$name,'trades'=>count($trades),'labeled'=>$labeled,'mtime'=>filemtime($f),'bytes'=>filesize($f)];
        }
        usort($out, fn($a,$b) => $b['mtime'] <=> $a['mtime']);
        echo json_encode($out);
        exit;
    }

    if ($_GET['api'] === 'ml_model_status') {
        $pkl  = $DATA_DIR . '/ml_filter.pkl';
        $meta = $DATA_DIR . '/ml_filter.json';
        if (!file_exists($pkl)) { echo json_encode(['trained'=>false]); exit; }
        $resp = ['trained'=>true,'pkl_mtime'=>filemtime($pkl),'pkl_bytes'=>filesize($pkl)];
        if (file_exists($meta)) {
            $m = json_decode(@file_get_contents($meta), true);
            if (is_array($m)) $resp['meta'] = $m;
        }
        echo json_encode($resp);
        exit;
    }

    if ($_GET['api'] === 'ml_train' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $model    = in_array($body['model'] ?? '', ['logreg','gbm']) ? $body['model'] : 'logreg';
        $thr      = floatval($body['threshold'] ?? 0.50);
        $testFrac = floatval($body['test_frac'] ?? 0.20);
        $minTr    = intval($body['min_trades']  ?? 200);
        $histWt   = floatval($body['history_weight'] ?? 0.5);
        $noHist   = !empty($body['no_history']);
        $incRaw   = $body['include'] ?? [];
        $include  = [];
        if (is_array($incRaw)) {
            foreach ($incRaw as $name) {
                $b = basename((string)$name);
                if (preg_match('/^[A-Za-z0-9._-]+\.json$/', $b) && $b !== 'ml_filter.json') $include[] = $b;
            }
        }
        $cmd = sprintf(
            "cd %s && python3 train_filter.py --model %s --threshold %.3f --test-frac %.3f --min-trades %d --history-weight %.2f",
            escapeshellarg($BOT_DIR), escapeshellarg($model), $thr, $testFrac, $minTr, $histWt
        );
        if ($noHist) $cmd .= ' --no-history';
        if ($include) $cmd .= ' --include ' . escapeshellarg(implode(',', $include));
        $cmd .= ' 2>&1';
        $t0 = microtime(true); $output = []; $ret = -1;
        exec($cmd, $output, $ret);
        $elapsed = microtime(true) - $t0;
        $meta = null;
        $metaPath = $DATA_DIR . '/ml_filter.json';
        if ($ret === 0 && file_exists($metaPath)) $meta = json_decode(@file_get_contents($metaPath), true);
        echo json_encode(['success'=>$ret===0,'return_code'=>$ret,'elapsed_sec'=>round($elapsed,2),'command'=>$cmd,'output'=>implode("\n",$output),'meta'=>$meta]);
        exit;
    }

    if ($_GET['api'] === 'fetch_history' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $hours = floatval($body['hours'] ?? 48);
        $appId = intval($body['app_id'] ?? 1089);
        $cmd = sprintf("cd %s && python3 fetch_history.py --app-id %d --hours %.0f 2>&1", escapeshellarg($BOT_DIR), $appId, $hours);
        $t0 = microtime(true); $output = []; $ret = -1;
        exec($cmd, $output, $ret);
        echo json_encode(['success'=>$ret===0,'return_code'=>$ret,'elapsed_sec'=>round(microtime(true)-$t0,2),'command'=>$cmd,'output'=>implode("\n",$output)]);
        exit;
    }

    if ($_GET['api'] === 'scan_market') {
        header('Content-Type: text/event-stream');
        header('Cache-Control: no-cache');
        header('X-Accel-Buffering: no');
        if (ob_get_level()) ob_end_clean();
        $hours = floatval($_GET['hours'] ?? 1);
        $appId = intval($_GET['app_id'] ?? 1089);
        if ($hours < 0.25 || $hours > 24) $hours = 1;
        $cmd = sprintf("cd %s && python3 market_scanner.py --app-id %d --hours %.2f 2>/dev/null", escapeshellarg($BOT_DIR), $appId, $hours);
        $proc = popen($cmd, 'r');
        if (!$proc) { echo "data: " . json_encode(['type'=>'error','message'=>'Failed to start scanner']) . "\n\n"; flush(); exit; }
        while (!feof($proc)) {
            $line = fgets($proc);
            if ($line === false) break;
            $line = trim($line);
            if ($line === '') continue;
            echo "data: $line\n\n"; flush();
        }
        pclose($proc);
        exit;
    }

    if ($_GET['api'] === 'daemon_start' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $interval = intval($body['interval'] ?? 30);
        $appId    = intval($body['app_id']   ?? 1089);
        $hours    = isset($body['hours']) ? floatval($body['hours']) : 1.0;
        if ($interval < 5) $interval = 5;
        if ($interval > 300) $interval = 300;
        $DAEMON_TMUX = 'market_daemon';
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1", $chk, $chkCode);
        if ($chkCode === 0) {
            exec("tmux send-keys -t " . escapeshellarg($DAEMON_TMUX) . " C-c 2>&1");
            usleep(500000);
            exec("tmux kill-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1");
            sleep(1);
        }
        $cmd = sprintf("cd %s && python3 market_daemon.py --app-id %d --interval %d --hours %.4f", escapeshellarg($BOT_DIR), $appId, $interval, $hours);
        $tmuxCmd = "tmux new-session -d -s " . escapeshellarg($DAEMON_TMUX) . " " . escapeshellarg($cmd) . " 2>&1";
        exec($tmuxCmd, $out, $ret);
        sleep(2);
        $v=[]; $vc=-1;
        exec("tmux has-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1", $v, $vc);
        echo json_encode(['success'=>($vc===0),'interval'=>$interval]);
        exit;
    }

    if ($_GET['api'] === 'daemon_stop' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $DAEMON_TMUX = 'market_daemon';
        exec("tmux send-keys -t " . escapeshellarg($DAEMON_TMUX) . " C-c 2>&1");
        usleep(500000);
        exec("tmux kill-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1");
        echo json_encode(['success'=>true]);
        exit;
    }

    if ($_GET['api'] === 'daemon_status') {
        $DAEMON_TMUX = 'market_daemon';
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1", $chk, $chkCode);
        $scanFile = $BOT_DIR . '/data/market_scan.json';
        $fileAge = file_exists($scanFile) ? time() - filemtime($scanFile) : null;
        echo json_encode(['running'=>($chkCode===0),'file_exists'=>file_exists($scanFile),'file_age_seconds'=>$fileAge]);
        exit;
    }

    if ($_GET['api'] === 'daemon_scan') {
        $scanFile = $BOT_DIR . '/data/market_scan.json';
        if (!file_exists($scanFile)) { echo json_encode(['error'=>'No scan data. Start the Market Daemon first.']); exit; }
        header('Content-Type: application/json');
        echo file_get_contents($scanFile);
        exit;
    }

    // Background processes status
    if ($_GET['api'] === 'bg_status') {
        $DAEMON_TMUX = 'market_daemon';
        $botChk=[]; $botCode=-1;
        exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $botChk, $botCode);
        $daemonChk=[]; $daemonCode=-1;
        exec("tmux has-session -t " . escapeshellarg($DAEMON_TMUX) . " 2>&1", $daemonChk, $daemonCode);
        $scanFile = $BOT_DIR . '/data/market_scan.json';
        echo json_encode([
            'bot_running'    => $botCode === 0,
            'daemon_running' => $daemonCode === 0,
            'daemon_file_age'=> file_exists($scanFile) ? time() - filemtime($scanFile) : null,
        ]);
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
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  /* Primary palette — matches reference: whites, blues, teals */
  --bg:#f0f4f8;
  --surface:#ffffff;
  --surface2:#f7fafc;
  --surface3:#edf2f7;
  --border:#e2e8f0;
  --border2:#cbd5e0;

  /* Text */
  --text:#1a202c;
  --text2:#4a5568;
  --text3:#718096;
  --text4:#a0aec0;

  /* Accents */
  --blue:#2b6cb0;
  --blue-light:#3182ce;
  --blue-bg:#ebf8ff;
  --blue-border:#bee3f8;
  --teal:#319795;
  --teal-bg:#e6fffa;
  --teal-border:#81e6d9;
  --green:#276749;
  --green-light:#38a169;
  --green-bg:#f0fff4;
  --green-border:#9ae6b4;
  --red:#c53030;
  --red-light:#e53e3e;
  --red-bg:#fff5f5;
  --red-border:#feb2b2;
  --amber:#b7791f;
  --amber-bg:#fffbeb;
  --amber-border:#fcd34d;
  --purple:#553c9a;
  --purple-bg:#faf5ff;

  /* Sidebar */
  --sidebar-bg:#1a365d;
  --sidebar-text:#e2e8f0;
  --sidebar-text2:#a0aec0;
  --sidebar-active:#2b6cb0;
  --sidebar-hover:#2a4a7f;

  --font:'Plus Jakarta Sans',sans-serif;
  --mono:'IBM Plex Mono',monospace;
  --radius:12px;
  --radius-sm:8px;
  --radius-lg:16px;
  --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.05);
  --shadow-md:0 4px 12px rgba(0,0,0,.08),0 2px 4px rgba(0,0,0,.05);
  --shadow-lg:0 10px 25px rgba(0,0,0,.1),0 4px 10px rgba(0,0,0,.06);
}

html{font-size:14px}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;display:flex;overflow-x:hidden}

/* ── SIDEBAR ── */
.sidebar{width:220px;min-height:100vh;background:var(--sidebar-bg);display:flex;flex-direction:column;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto}
.sidebar-logo{padding:24px 20px 20px;border-bottom:1px solid rgba(255,255,255,.08)}
.sidebar-logo-text{font-size:1.15rem;font-weight:800;color:#fff;letter-spacing:-.02em;line-height:1.2}
.sidebar-logo-sub{font-size:.72rem;color:var(--sidebar-text2);margin-top:2px;font-weight:400}
.sidebar-section{padding:8px 12px 4px;font-size:.6rem;font-weight:700;color:var(--sidebar-text2);text-transform:uppercase;letter-spacing:.1em;margin-top:8px}
.sidebar-item{display:flex;align-items:center;gap:10px;padding:9px 14px;margin:1px 8px;border-radius:var(--radius-sm);cursor:pointer;color:var(--sidebar-text2);font-size:.82rem;font-weight:500;transition:all .15s;position:relative}
.sidebar-item:hover{background:var(--sidebar-hover);color:#fff}
.sidebar-item.active{background:var(--sidebar-active);color:#fff}
.sidebar-item .si-icon{width:18px;text-align:center;flex-shrink:0;font-size:.9rem;opacity:.85}
.sidebar-item .si-badge{margin-left:auto;background:rgba(255,255,255,.15);color:#fff;font-size:.6rem;font-weight:700;padding:2px 6px;border-radius:10px;font-family:var(--mono)}
.sidebar-item .si-dot{width:7px;height:7px;border-radius:50%;margin-left:auto;flex-shrink:0}
.sidebar-item .si-dot.on{background:#68d391;box-shadow:0 0 6px #68d391;animation:pulse-dot 2s infinite}
.sidebar-item .si-dot.off{background:#fc8181}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.5}}
.sidebar-footer{margin-top:auto;padding:12px;border-top:1px solid rgba(255,255,255,.08)}
.sidebar-footer-text{font-size:.67rem;color:var(--sidebar-text2);line-height:1.5}

/* ── MAIN ── */
.main{flex:1;display:flex;flex-direction:column;overflow-x:hidden;min-width:0}
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.topbar-title{font-size:1.1rem;font-weight:700;color:var(--text)}
.topbar-right{display:flex;align-items:center;gap:10px}
.topbar-badge{display:flex;align-items:center;gap:5px;padding:5px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:20px;font-size:.73rem;font-weight:600;color:var(--text2)}
.topbar-badge .dot{width:6px;height:6px;border-radius:50%}
.topbar-badge .dot.on{background:var(--green-light);animation:pulse-dot 2s infinite}
.topbar-badge .dot.off{background:var(--red-light)}

.content{padding:24px 28px;flex:1}
.page-header{margin-bottom:20px}
.page-header h2{font-size:1.25rem;font-weight:700;color:var(--text)}
.page-header p{font-size:.82rem;color:var(--text3);margin-top:3px}

/* ── BUTTONS ── */
.btn{font-family:var(--font);font-size:.8rem;font-weight:600;padding:8px 16px;border-radius:var(--radius-sm);border:none;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;letter-spacing:.01em}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none!important}
.btn-primary{background:var(--blue-light);color:#fff;box-shadow:var(--shadow)}
.btn-primary:hover:not(:disabled){background:var(--blue);transform:translateY(-1px);box-shadow:var(--shadow-md)}
.btn-danger{background:var(--red-light);color:#fff;box-shadow:var(--shadow)}
.btn-danger:hover:not(:disabled){background:var(--red);transform:translateY(-1px)}
.btn-ghost{background:var(--surface);color:var(--text2);border:1px solid var(--border);box-shadow:var(--shadow)}
.btn-ghost:hover:not(:disabled){background:var(--surface2);color:var(--text);border-color:var(--border2)}
.btn-teal{background:var(--teal);color:#fff;box-shadow:var(--shadow)}
.btn-teal:hover:not(:disabled){background:#2c7a7b;transform:translateY(-1px)}
.btn-sm{padding:5px 12px;font-size:.75rem}

/* ── CARDS ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.card-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.card-header h3{font-size:.88rem;font-weight:700;color:var(--text)}
.card-body{padding:20px}

/* ── SESSION LIST ── */
.sessions-scroll{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px;margin-bottom:24px}
.sessions-scroll::-webkit-scrollbar{height:4px}
.sessions-scroll::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.session-card{flex:0 0 260px;background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:14px 16px;cursor:pointer;transition:all .15s;position:relative}
.session-card:hover{border-color:var(--blue-light);box-shadow:var(--shadow-md);transform:translateY(-1px)}
.session-card.active-card{border-color:var(--blue-light);box-shadow:0 0 0 2px var(--blue-bg),var(--shadow-md)}
.session-card.live-card::after{content:'LIVE';position:absolute;top:10px;right:10px;background:var(--green-light);color:#fff;font-size:.58rem;font-weight:800;padding:2px 6px;border-radius:20px;letter-spacing:.05em;animation:pulse-live 2s infinite}
@keyframes pulse-live{0%,100%{opacity:1}50%{opacity:.6}}
.sc-mode{font-size:.63rem;font-weight:700;padding:2px 7px;border-radius:10px;text-transform:uppercase;letter-spacing:.05em;display:inline-block;margin-bottom:6px}
.sc-mode.demo{background:var(--blue-bg);color:var(--blue-light)}
.sc-mode.live{background:var(--green-bg);color:var(--green-light)}
.sc-id{font-size:.88rem;font-weight:700;color:var(--text);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sc-date{font-size:.68rem;color:var(--text3);margin-bottom:10px}
.sc-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.sc-stat{display:flex;flex-direction:column;gap:1px}
.sc-stat .k{font-size:.6rem;color:var(--text4);text-transform:uppercase;letter-spacing:.05em}
.sc-stat .v{font-family:var(--mono);font-size:.8rem;font-weight:600;color:var(--text)}
.c-green{color:var(--green-light)}.c-red{color:var(--red-light)}.c-blue{color:var(--blue-light)}

/* ── STAT CARDS ── */
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow);position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.stat-card.blue::before{background:linear-gradient(90deg,var(--blue-light),var(--teal))}
.stat-card.green::before{background:linear-gradient(90deg,var(--green-light),var(--teal))}
.stat-card.red::before{background:linear-gradient(90deg,var(--red-light),var(--amber))}
.stat-card.purple::before{background:linear-gradient(90deg,var(--purple),var(--blue-light))}
.stat-card.teal::before{background:linear-gradient(90deg,var(--teal),var(--blue-light))}
.stat-card.amber::before{background:linear-gradient(90deg,var(--amber),var(--red-light))}
.stat-card .label{font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:8px}
.stat-card .value{font-family:var(--mono);font-size:1.4rem;font-weight:700;color:var(--text);line-height:1}
.stat-card .sub{font-size:.72rem;color:var(--text3);margin-top:5px;font-weight:500}
.stat-card .icon{position:absolute;top:14px;right:14px;width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:1rem}
.stat-card.blue .icon{background:var(--blue-bg);color:var(--blue-light)}
.stat-card.green .icon{background:var(--green-bg);color:var(--green-light)}
.stat-card.red .icon{background:var(--red-bg);color:var(--red-light)}
.stat-card.purple .icon{background:var(--purple-bg);color:var(--purple)}
.stat-card.teal .icon{background:var(--teal-bg);color:var(--teal)}
.stat-card.amber .icon{background:var(--amber-bg);color:var(--amber)}

/* ── CHARTS ── */
.chart-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:1100px){.chart-row{grid-template-columns:1fr 1fr}}
@media(max-width:700px){.chart-row{grid-template-columns:1fr}}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.chart-card .ch-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.chart-card .ch-head h4{font-size:.82rem;font-weight:700;color:var(--text)}
.chart-card .ch-head .ch-sub{font-size:.7rem;color:var(--text3)}
.chart-body{padding:16px}
canvas{width:100%!important}

/* ── STREAK ── */
.streak-bar{display:flex;width:100%;height:28px;border-radius:var(--radius-sm);overflow:hidden;background:var(--surface3)}
.streak-seg{display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:.62rem;font-weight:700;min-width:8px;color:#fff;position:relative;cursor:pointer}
.streak-seg.win{background:#38a169}.streak-seg.loss{background:#e53e3e}
.streak-seg:hover{opacity:.8}
.streak-tip{position:absolute;bottom:calc(100%+6px);left:50%;transform:translateX(-50%);background:var(--text);color:#fff;border-radius:6px;padding:5px 8px;font-size:.67rem;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .15s;z-index:20}
.streak-seg:hover .streak-tip{opacity:1}

/* ── SYMBOL GRID ── */
.symbol-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
.sym-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px 14px;box-shadow:var(--shadow)}
.sym-name{font-family:var(--mono);font-size:.85rem;font-weight:700;color:var(--text);margin-bottom:8px}
.sym-row{display:flex;gap:10px;flex-wrap:wrap}
.sym-stat{display:flex;flex-direction:column;gap:1px;min-width:45px}
.sym-stat .k{font-size:.58rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text4)}
.sym-stat .v{font-family:var(--mono);font-size:.78rem;font-weight:600}
.sym-bar{height:4px;border-radius:2px;background:var(--surface3);overflow:hidden;margin-top:8px}
.sym-bar-fill{height:100%;border-radius:2px;transition:width .4s}

/* ── TRADE TABLE ── */
.trade-table-wrap{overflow-x:auto;max-height:440px;overflow-y:auto}
.trade-table{width:100%;border-collapse:collapse;min-width:700px;font-size:.78rem}
.trade-table thead th{position:sticky;top:0;background:var(--surface2);border-bottom:2px solid var(--border);padding:10px 14px;text-align:left;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700;z-index:2}
.trade-table tbody td{padding:9px 14px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:.76rem}
.trade-table tbody tr:hover td{background:var(--surface2)}
.trade-table tbody tr:last-child td{border-bottom:none}

/* ── BADGES ── */
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:.65rem;font-weight:700;letter-spacing:.02em}
.badge-win{background:var(--green-bg);color:var(--green-light)}
.badge-loss{background:var(--red-bg);color:var(--red-light)}
.badge-even{background:var(--blue-bg);color:var(--blue-light)}
.badge-odd{background:var(--purple-bg);color:var(--purple)}
.badge-call{background:var(--teal-bg);color:var(--teal)}
.badge-put{background:var(--amber-bg);color:var(--amber)}
.badge-over{background:var(--green-bg);color:var(--green)}
.badge-under{background:var(--red-bg);color:var(--red)}
.badge-touch{background:var(--blue-bg);color:var(--blue-light)}
.badge-notouch{background:var(--purple-bg);color:var(--purple)}
.badge-mode-demo{background:var(--blue-bg);color:var(--blue-light);font-size:.65rem;padding:2px 8px;border-radius:20px;font-weight:700}
.badge-mode-live{background:var(--green-bg);color:var(--green-light);font-size:.65rem;padding:2px 8px;border-radius:20px;font-weight:700}

/* ── CONFIG INFO BAR ── */
.config-info{display:flex;flex-wrap:wrap;gap:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:20px;overflow:hidden}
.ci-item{padding:12px 18px;border-right:1px solid var(--border);flex:1;min-width:120px}
.ci-item:last-child{border-right:none}
.ci-item .k{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:4px}
.ci-item .v{font-family:var(--mono);font-size:.82rem;font-weight:600;color:var(--text)}
.config-syms{display:flex;gap:4px;flex-wrap:wrap}
.sym-tag{font-family:var(--mono);font-size:.62rem;padding:1px 6px;border-radius:4px;background:var(--blue-bg);border:1px solid var(--blue-border);color:var(--blue-light);font-weight:600}

/* ── CONTROL PAGE ── */
.ctrl-layout{display:grid;grid-template-columns:380px 1fr;gap:20px;align-items:start}
@media(max-width:1000px){.ctrl-layout{grid-template-columns:1fr}}

/* Status pill */
.status-pill{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:var(--radius-sm);margin-bottom:14px}
.status-pill.running{background:var(--green-bg);border:1px solid var(--green-border)}
.status-pill.stopped{background:var(--red-bg);border:1px solid var(--red-border)}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.status-dot.on{background:var(--green-light);box-shadow:0 0 0 3px rgba(56,161,105,.2);animation:pulse-dot 2s infinite}
.status-dot.off{background:var(--red-light)}
.status-text{font-weight:700;font-size:.88rem}
.status-pill.running .status-text{color:var(--green)}
.status-pill.stopped .status-text{color:var(--red)}

/* Form elements */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-full{grid-column:1/-1}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)}
.form-group input,.form-group select{font-family:var(--mono);font-size:.83rem;padding:8px 11px;border-radius:var(--radius-sm);border:1.5px solid var(--border);background:var(--surface);color:var(--text);outline:none;transition:border-color .15s;width:100%}
.form-group input:focus,.form-group select:focus{border-color:var(--blue-light);box-shadow:0 0 0 2px var(--blue-bg)}
.form-group .hint{font-size:.63rem;color:var(--text4)}

/* Mode toggle */
.mode-group{display:flex;border:1.5px solid var(--border);border-radius:var(--radius-sm);overflow:hidden}
.mode-btn{font-family:var(--font);font-size:.78rem;font-weight:700;padding:7px 0;border:none;background:var(--surface2);color:var(--text3);cursor:pointer;flex:1;transition:all .15s}
.mode-btn.active-demo{background:var(--blue-bg);color:var(--blue-light)}
.mode-btn.active-real{background:var(--green-bg);color:var(--green-light)}

/* Toggle switch */
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--border)}
.toggle-row:last-child{border-bottom:none}
.toggle-label{font-size:.8rem;font-weight:600;color:var(--text)}
.toggle-sub{font-size:.68rem;color:var(--text3);margin-top:1px}
.toggle{position:relative;width:38px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:var(--surface3);border:1.5px solid var(--border2);border-radius:11px;cursor:pointer;transition:.2s}
.toggle-slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:2px;background:var(--text4);border-radius:50%;transition:.2s}
.toggle input:checked+.toggle-slider{background:var(--blue-bg);border-color:var(--blue-light)}
.toggle input:checked+.toggle-slider::before{transform:translateX(16px);background:var(--blue-light)}

/* Cmd preview */
.cmd-preview{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;font-family:var(--mono);font-size:.7rem;color:var(--text2);word-break:break-all;line-height:1.65;max-height:80px;overflow-y:auto;margin-top:12px}
.cmd-preview .cmd-hl{color:var(--blue-light);font-weight:600}

/* SSE Log */
.log-output{background:#0f1923;border-radius:var(--radius-sm);padding:14px;font-family:var(--mono);font-size:.72rem;color:#68d391;line-height:1.7;max-height:500px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;border:1px solid #2d3748}
.log-output.empty{color:#4a5568;font-style:italic}
.log-sse-status{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:.68rem;color:var(--text3)}
.sse-dot{width:6px;height:6px;border-radius:50%;background:var(--green-light);animation:pulse-dot 2s infinite}
.sse-dot.off{background:var(--text4);animation:none}

/* ML Training */
.ml-status-card{padding:12px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);display:flex;align-items:center;gap:10px;margin-bottom:14px}
.ml-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.ml-dot.trained{background:var(--green-light);box-shadow:0 0 0 3px rgba(56,161,105,.2)}
.ml-dot.untrained{background:var(--text4)}
.ml-meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:14px}
.ml-meta-item{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 10px}
.ml-meta-item .lbl{font-size:.6rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:3px}
.ml-meta-item .val{font-family:var(--mono);font-size:.82rem;font-weight:600;color:var(--text)}
.ml-output{background:#0f1923;border-radius:var(--radius-sm);padding:12px;font-family:var(--mono);font-size:.71rem;color:#68d391;line-height:1.6;max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;border:1px solid #2d3748}
.ml-output.empty{color:#4a5568;font-style:italic}
.file-list{max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface2)}
.file-item{display:flex;align-items:center;gap:8px;padding:7px 10px;font-size:.77rem;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
.file-item:last-child{border-bottom:none}
.file-item:hover{background:var(--surface3)}
.file-item input{accent-color:var(--blue-light)}
.file-item .fname{font-family:var(--mono);color:var(--text);flex:1}
.file-item .fmeta{font-family:var(--mono);font-size:.66rem;color:var(--text3)}
.ml-thr-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.74rem}
.ml-thr-table th{text-align:left;padding:7px 10px;font-size:.63rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);border-bottom:2px solid var(--border);font-weight:700}
.ml-thr-table td{padding:7px 10px;border-bottom:1px solid var(--border)}

/* Scanner */
.scan-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.scan-card{background:var(--surface);border:1.5px solid var(--border);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow);transition:all .15s}
.scan-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}
.scan-card.strong{border-color:var(--green-light);background:var(--green-bg)}
.scan-card.good{border-color:var(--teal);background:var(--teal-bg)}
.scan-card.wait{border-color:var(--amber);background:var(--amber-bg)}
.scan-card.danger{border-color:var(--red-light);background:var(--red-bg);opacity:.75}
.scan-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.scan-sym{font-family:var(--mono);font-size:1rem;font-weight:700;color:var(--text)}
.scan-badge{font-size:.62rem;font-weight:700;padding:3px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.04em}
.scan-badge.strong-entry{background:var(--green-bg);color:var(--green);border:1px solid var(--green-border)}
.scan-badge.good-entry{background:var(--teal-bg);color:var(--teal);border:1px solid var(--teal-border)}
.scan-badge.wait-badge{background:var(--amber-bg);color:var(--amber);border:1px solid var(--amber-border)}
.scan-badge.no-entry{background:var(--red-bg);color:var(--red-light);border:1px solid var(--red-border)}
.scan-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.scan-metric .sm-label{font-size:.58rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:2px}
.scan-metric .sm-value{font-family:var(--mono);font-size:.8rem;font-weight:600;color:var(--text)}
.scan-patterns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.scan-pat{font-family:var(--mono);font-size:.68rem;padding:3px 7px;border-radius:20px;background:var(--surface2);border:1px solid var(--border);color:var(--text3)}
.scan-pat.active{background:var(--blue-bg);border-color:var(--blue-border);color:var(--blue-light)}
.scan-rec{background:var(--surface2);border-radius:var(--radius-sm);padding:8px 10px;display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.scan-rec-text{font-size:.76rem;color:var(--text2)}
.scan-use-btn{font-size:.7rem;font-weight:700;padding:5px 12px;border-radius:20px;border:1.5px solid var(--blue-light);background:transparent;color:var(--blue-light);cursor:pointer;transition:all .15s;font-family:var(--font)}
.scan-use-btn:hover{background:var(--blue-light);color:#fff}
.trad-bar{height:4px;border-radius:2px;background:var(--surface3);overflow:hidden;margin-top:6px}
.trad-fill{height:100%;border-radius:2px;transition:width .4s}

/* Daemon section */
.daemon-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);margin-bottom:16px}
.daemon-box h3{font-size:.88rem;font-weight:700;margin-bottom:4px;color:var(--text)}
.daemon-box .sub{font-size:.76rem;color:var(--text3);margin-bottom:14px;line-height:1.5}

/* BG Status bar */
.bg-status-bar{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 14px;margin-bottom:16px;box-shadow:var(--shadow)}
.bgs-item{display:flex;align-items:center;gap:5px;font-size:.73rem;font-weight:600;color:var(--text2)}
.bgs-item .dot{width:7px;height:7px;border-radius:50%}
.bgs-item .dot.on{background:var(--green-light);animation:pulse-dot 2s infinite}
.bgs-item .dot.off{background:var(--text4)}
.bgs-sep{width:1px;height:14px;background:var(--border);margin:0 4px}
.bgs-note{font-size:.68rem;color:var(--text4);margin-left:auto}

/* Empty state */
.empty-state{text-align:center;padding:60px 20px;color:var(--text3)}
.empty-state .icon{font-size:2.5rem;margin-bottom:12px;opacity:.5}
.empty-state p{font-size:.9rem;max-width:380px;margin:0 auto;line-height:1.6;color:var(--text3)}

/* Scroll */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}

/* Spinner */
.spinner{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--blue-light);border-radius:50%;animation:spin .6s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}

/* Section header */
.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.section-head h3{font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3)}

/* Tab content */
.tab-content{display:none}
.tab-content.active{display:block}

/* Progress bar */
.prog-wrap{display:flex;align-items:center;gap:8px;font-size:.75rem;color:var(--text3)}
.prog-bar{width:160px;height:5px;background:var(--surface3);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--blue-light),var(--teal));border-radius:3px;transition:width .3s}

/* Algo/TS filter checkboxes */
.filter-checks{display:flex;flex-wrap:wrap;gap:6px}
.filter-check-label{display:flex;align-items:center;gap:4px;padding:4px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:20px;cursor:pointer;font-size:.73rem;font-weight:600;color:var(--text2);transition:all .12s}
.filter-check-label:hover{border-color:var(--blue-light);color:var(--blue-light)}
.filter-check-label input{accent-color:var(--blue-light)}

/* Responsive */
@media(max-width:768px){
  .sidebar{display:none}
  .stat-row{grid-template-columns:1fr 1fr}
  .content{padding:16px}
  .chart-row{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- SIDEBAR -->
<nav class="sidebar">
  <div class="sidebar-logo">
    <div class="sidebar-logo-text">DerivBot</div>
    <div class="sidebar-logo-sub">Multi-Symbol Strategy Monitor</div>
  </div>
  <div class="sidebar-section">Analytics</div>
  <div class="sidebar-item active" data-tab="analytics" onclick="switchTab('analytics',this)">
    <span class="si-icon">📊</span> Sessions
    <span class="si-badge" id="sessCount">0</span>
  </div>
  <div class="sidebar-section">Bot</div>
  <div class="sidebar-item" data-tab="control" onclick="switchTab('control',this)">
    <span class="si-icon">🤖</span> Bot Control
    <span class="si-dot off" id="sidebarBotDot"></span>
  </div>
  <div class="sidebar-section">Intelligence</div>
  <div class="sidebar-item" data-tab="training" onclick="switchTab('training',this)">
    <span class="si-icon">🧠</span> ML Training
    <span class="si-dot off" id="sidebarMlDot"></span>
  </div>
  <div class="sidebar-item" data-tab="scanner" onclick="switchTab('scanner',this)">
    <span class="si-icon">🔍</span> Market Scanner
    <span class="si-dot off" id="sidebarDaemonDot"></span>
  </div>
  <div class="sidebar-footer">
    <div class="sidebar-footer-text">
      <strong>Processes</strong><br>
      Bot: <span id="sfBotStatus" style="color:#fc8181">stopped</span><br>
      Daemon: <span id="sfDaemonStatus" style="color:#fc8181">stopped</span>
    </div>
  </div>
</nav>

<!-- MAIN -->
<div class="main">
  <!-- TOP BAR -->
  <div class="topbar">
    <div class="topbar-title" id="topbarTitle">Session Analytics</div>
    <div class="topbar-right">
      <div class="topbar-badge">
        <span class="dot off" id="topBotDot"></span> Bot
      </div>
      <div class="topbar-badge">
        <span class="dot off" id="topDaemonDot"></span> Daemon
      </div>
      <label style="display:flex;align-items:center;gap:5px;font-size:.75rem;color:var(--text2);cursor:pointer">
        <input type="checkbox" id="autoRefresh" style="accent-color:var(--blue-light)"> Auto 10s
      </label>
      <button class="btn btn-ghost btn-sm" onclick="refreshAll()">↻ Refresh</button>
    </div>
  </div>

  <div class="content">

    <!-- ═══════════════ ANALYTICS TAB ═══════════════ -->
    <div class="tab-content active" id="tab-analytics">
      <div class="page-header">
        <h2>Trading Sessions</h2>
        <p>Click a session card to view detailed analytics and trade history</p>
      </div>

      <div class="sessions-scroll" id="sessionGrid"></div>

      <div class="empty-state" id="emptyState">
        <div class="icon">📈</div>
        <p>Select a session above to view equity curves, trade breakdown, symbol performance, and full trade log.</p>
      </div>

      <div id="dashboard" style="display:none">
        <!-- Config info -->
        <div class="config-info" id="configBar"></div>

        <!-- Stats -->
        <div class="stat-row" id="statsRow"></div>

        <!-- Charts: Equity + Stake + Win/Loss donut -->
        <div class="chart-row" id="chartRow">
          <div class="chart-card" style="grid-column:span 2">
            <div class="ch-head"><h4>Equity Curve & P&L</h4><span class="ch-sub" id="eqChartSub"></span></div>
            <div class="chart-body" style="height:220px"><canvas id="equityChart"></canvas></div>
          </div>
          <div class="chart-card">
            <div class="ch-head"><h4>Win / Loss</h4></div>
            <div class="chart-body" style="height:220px"><canvas id="wlChart"></canvas></div>
          </div>
        </div>
        <div class="chart-row" style="grid-template-columns:1fr 1fr">
          <div class="chart-card">
            <div class="ch-head"><h4>Stake Progression</h4><span class="ch-sub">Martingale pattern</span></div>
            <div class="chart-body" style="height:160px"><canvas id="stakeChart"></canvas></div>
          </div>
          <div class="chart-card">
            <div class="ch-head"><h4>Win/Loss Streak Map</h4></div>
            <div class="chart-body">
              <div class="streak-bar" id="streakBar"></div>
              <div style="overflow-x:auto;margin-top:12px">
                <table class="trade-table">
                  <thead><tr><th>Type</th><th>Length</th><th>Trades</th><th>P&L Impact</th></tr></thead>
                  <tbody id="streakBody"></tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <!-- Symbol Breakdown -->
        <div class="card" style="margin-bottom:20px">
          <div class="card-header"><h3>Symbol Breakdown</h3></div>
          <div class="card-body"><div class="symbol-grid" id="symbolGrid"></div></div>
        </div>

        <!-- Trade Log -->
        <div class="card">
          <div class="card-header">
            <h3>Trade Log</h3>
            <span style="font-size:.75rem;color:var(--text3)" id="tradeCount"></span>
          </div>
          <div class="trade-table-wrap">
            <table class="trade-table">
              <thead><tr><th>#</th><th>Time</th><th>Symbol</th><th>Type</th><th>Result</th><th>Stake</th><th>Profit</th><th>Payout</th><th>Cum P&L</th><th>Equity</th></tr></thead>
              <tbody id="tradeBody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════ CONTROL TAB ═══════════════ -->
    <div class="tab-content" id="tab-control">
      <div class="page-header">
        <h2>Bot Control</h2>
        <p>Configure and manage the trading bot running in tmux session <code>bbot</code></p>
      </div>

      <div class="ctrl-layout">
        <!-- LEFT COLUMN -->
        <div>
          <div id="statusPill" class="status-pill stopped">
            <div class="status-dot off" id="statusDot"></div>
            <div>
              <div class="status-text" id="statusLabel">Stopped</div>
              <div style="font-size:.7rem;color:var(--text3);margin-top:1px">tmux: bbot</div>
            </div>
            <div style="margin-left:auto;display:flex;gap:8px">
              <button class="btn btn-danger btn-sm" id="stopBtn" onclick="stopBot()" disabled>⏹ Stop</button>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><h3>⚙ Configure & Start</h3></div>
            <div class="card-body">
              <div style="margin-bottom:14px">
                <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:6px">Account Mode</div>
                <div class="mode-group">
                  <button class="mode-btn active-demo" id="modeDemo" onclick="setMode('demo')">Demo</button>
                  <button class="mode-btn" id="modeReal" onclick="setMode('real')">Real</button>
                </div>
              </div>
              <div class="form-grid">
                <div class="form-group form-full">
                  <label>API Token</label>
                  <input type="text" id="fToken" placeholder="Leave empty for default token" oninput="updateCmd()">
                </div>
                <div class="form-group">
                  <label>Base Stake ($)</label>
                  <input type="number" id="fStake" value="0.35" step="0.01" min="0.35" oninput="updateCmd()">
                  <span class="hint">Min $0.35</span>
                </div>
                <div class="form-group">
                  <label>Martingale ×</label>
                  <input type="number" id="fMartingale" value="2.2" step="0.1" min="1" oninput="updateCmd()">
                </div>
                <div class="form-group">
                  <label>Max Stake ($)</label>
                  <input type="number" id="fMaxStake" value="50" step="1" min="1" oninput="updateCmd()">
                  <span class="hint" id="hMaxStake">Covers up to 7 losses</span>
                </div>
                <div class="form-group">
                  <label>Score Threshold</label>
                  <input type="number" id="fThreshold" value="0.60" step="0.01" min="0" max="1" oninput="updateCmd()">
                </div>
                <div class="form-group form-full">
                  <label>Algorithm</label>
                  <select id="fStrategy" onchange="onStrategyChange();updateCmd()">
                    <option value="alphabloom" selected>AlphaBloom</option>
                    <option value="pulse">Pulse (tri-timeframe)</option>
                    <option value="ensemble">Ensemble</option>
                    <option value="novaburst">NovaBurst (multi-layer)</option>
                    <option value="adaptive">Adaptive (Pulse + ML + hotness + vol)</option>
                  </select>
                </div>
                <div class="form-group form-full">
                  <label>Trade Strategy</label>
                  <select id="fTradeStrategy" onchange="updateCmd()">
                    <option value="even_odd" selected>Even/Odd (Digit Frequency)</option>
                    <option value="rise_fall_roll">Rise/Fall — Roll Cake</option>
                    <option value="rise_fall_zigzag">Rise/Fall — Zigzag 7 Ticks</option>
                    <option value="higher_lower_roll">Higher/Lower — Roll Cake</option>
                    <option value="higher_lower_zigzag">Higher/Lower — Zigzag 7 Ticks</option>
                    <option value="over_under_roll">Over/Under — Roll Cake</option>
                    <option value="touch_notouch_zigzag">Touch/No Touch — Zigzag 7 Ticks</option>
                  </select>
                  <span class="hint" id="hTradeStrategy">Contract type + pattern</span>
                </div>
                <div class="form-group" id="abWindowGroup">
                  <label>AB Window (ticks)</label>
                  <input type="number" id="fAbWindow" value="60" step="5" min="10" oninput="updateCmd()">
                </div>
                <div class="form-group" id="mlThresholdGroup" style="display:none">
                  <label>ML Threshold</label>
                  <input type="number" id="fMlThreshold" value="0.45" step="0.01" min="0" max="1" oninput="updateCmd()">
                  <span class="hint">P(win) cutoff</span>
                </div>
                <div class="form-group" id="hotnessColdGroup" style="display:none">
                  <label>Hotness Cold Cutoff</label>
                  <input type="number" id="fHotnessCold" value="0.43" step="0.01" oninput="updateCmd()">
                </div>
                <div class="form-group" id="hotnessProbeGroup" style="display:none">
                  <label>Hotness Probe Interval</label>
                  <input type="number" id="fHotnessProbe" value="20" step="1" oninput="updateCmd()">
                </div>
                <div class="form-group" id="mlIdleGroup" style="display:none">
                  <label>ML Idle Bypass (min)</label>
                  <input type="number" id="fMlIdle" value="10" step="1" oninput="updateCmd()">
                </div>
                <div class="form-group" id="mlFloorGroup" style="display:none">
                  <label>ML Floor Threshold</label>
                  <input type="number" id="fMlFloor" value="0.35" step="0.01" oninput="updateCmd()">
                </div>
                <div class="form-group" id="volSkipGroup" style="display:none">
                  <label>Vol Skip Percentile</label>
                  <input type="number" id="fVolSkip" value="0.75" step="0.05" oninput="updateCmd()">
                </div>
                <div class="form-group">
                  <label>Take Profit ($) <span style="color:var(--text4);font-weight:400">opt</span></label>
                  <input type="number" id="fProfit" step="1" min="0" placeholder="e.g. 50" oninput="updateCmd()">
                </div>
                <div class="form-group">
                  <label>Loss Limit ($) <span style="color:var(--text4);font-weight:400">opt</span></label>
                  <input type="number" id="fLoss" step="1" placeholder="e.g. -30" oninput="updateCmd()">
                </div>
              </div>

              <!-- Symbol selection -->
              <div style="margin-top:14px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                  <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">Symbols <span id="symSelectedCount" style="color:var(--text4);font-weight:400"></span></div>
                  <div style="display:flex;gap:5px">
                    <button type="button" onclick="symSelectAll(true)" class="btn btn-ghost btn-sm" style="padding:3px 9px;font-size:.67rem">All</button>
                    <button type="button" onclick="symSelectAll(false)" class="btn btn-ghost btn-sm" style="padding:3px 9px;font-size:.67rem">None</button>
                  </div>
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:5px" id="symChecklist"></div>
              </div>

              <!-- Toggles -->
              <div style="margin-top:14px;background:var(--surface2);border-radius:var(--radius-sm);padding:0 12px">
                <div class="toggle-row">
                  <div><div class="toggle-label">Disable Kelly Sizing</div><div class="toggle-sub">Base stake + martingale only</div></div>
                  <label class="toggle"><input type="checkbox" id="tKelly" checked onchange="updateCmd()"><span class="toggle-slider"></span></label>
                </div>
                <div class="toggle-row">
                  <div><div class="toggle-label">Disable Risk Engine</div><div class="toggle-sub">No cooldown or circuit breaker</div></div>
                  <label class="toggle"><input type="checkbox" id="tRisk" onchange="updateCmd()"><span class="toggle-slider"></span></label>
                </div>
                <div class="toggle-row">
                  <div><div class="toggle-label">ML Filter</div><div class="toggle-sub">Gate by trained P(win) model</div></div>
                  <label class="toggle"><input type="checkbox" id="tMl" onchange="updateCmd()"><span class="toggle-slider"></span></label>
                </div>
              </div>

              <div class="cmd-preview" id="cmdPreview"></div>

              <div style="margin-top:14px">
                <button class="btn btn-primary" id="startBtn" onclick="startBot()" style="width:100%">🚀 Start Bot</button>
              </div>
            </div>
          </div>
        </div>

        <!-- RIGHT COLUMN: Live Logs via SSE -->
        <div>
          <div class="card">
            <div class="card-header">
              <h3>Live Logs — bbot</h3>
              <div class="log-sse-status">
                <div class="sse-dot off" id="sseDot"></div>
                <span id="sseStatus">Disconnected</span>
                <button class="btn btn-ghost btn-sm" onclick="reconnectSSE()" style="margin-left:8px;padding:3px 10px;font-size:.68rem">↻</button>
              </div>
            </div>
            <div class="card-body" style="padding:14px">
              <div class="log-output empty" id="logOutput">Connecting to live log stream...</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════ ML TRAINING TAB ═══════════════ -->
    <div class="tab-content" id="tab-training">
      <div class="page-header">
        <h2>ML Training</h2>
        <p>Train and manage the P(win) filter model from trade history</p>
      </div>
      <div class="ctrl-layout">
        <div>
          <div class="card" style="margin-bottom:16px">
            <div class="card-header"><h3>🧠 Model Status</h3></div>
            <div class="card-body">
              <div class="ml-status-card" id="mlStatus">
                <div class="ml-dot untrained"></div>
                <div style="font-size:.8rem;color:var(--text)">Checking… <span style="color:var(--text3)">data/ml_filter.pkl</span></div>
              </div>
              <div class="ml-meta-grid" id="mlMetaGrid" style="display:none"></div>
              <div id="mlThresholdsWrap" style="display:none">
                <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:6px;margin-top:10px">Threshold Performance (Test Set)</div>
                <div style="overflow-x:auto"><table class="ml-thr-table" id="mlThrTable"></table></div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><h3>⚙ Train New Model</h3></div>
            <div class="card-body">
              <div class="form-grid">
                <div class="form-group">
                  <label>Model Type</label>
                  <select id="fMlModel">
                    <option value="logreg" selected>Logistic Regression</option>
                    <option value="gbm">Gradient Boosting</option>
                  </select>
                </div>
                <div class="form-group">
                  <label>Threshold</label>
                  <input type="number" id="fMlTrainThreshold" value="0.50" step="0.01" min="0" max="1">
                </div>
                <div class="form-group">
                  <label>Test Fraction</label>
                  <input type="number" id="fMlTestFrac" value="0.20" step="0.05" min="0.05" max="0.5">
                </div>
                <div class="form-group">
                  <label>Min Trades</label>
                  <input type="number" id="fMlMinTrades" value="200" step="50" min="50">
                </div>
                <div class="form-group form-full">
                  <label>History Weight</label>
                  <input type="number" id="fMlHistWeight" value="0.50" step="0.1" min="0.1" max="2.0">
                  <span class="hint">Weight for simulated ticks vs real trades (real=1.0)</span>
                </div>
              </div>
              <div style="margin-top:12px;background:var(--surface2);border-radius:var(--radius-sm);padding:0 12px">
                <div class="toggle-row">
                  <div><div class="toggle-label">Exclude Historical Data</div><div class="toggle-sub">Skip history-trades.json</div></div>
                  <label class="toggle"><input type="checkbox" id="tNoHistory"><span class="toggle-slider"></span></label>
                </div>
              </div>
              <div style="margin-top:14px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                  <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3)">Training Data <span id="mlFileCount" style="color:var(--text4);font-weight:400"></span></div>
                  <div style="display:flex;gap:5px">
                    <button type="button" onclick="mlSelectAll(true)" class="btn btn-ghost btn-sm" style="padding:3px 9px;font-size:.67rem">All</button>
                    <button type="button" onclick="mlSelectAll(false)" class="btn btn-ghost btn-sm" style="padding:3px 9px;font-size:.67rem">None</button>
                    <button type="button" onclick="loadMlFiles()" class="btn btn-ghost btn-sm" style="padding:3px 9px;font-size:.67rem">↻</button>
                  </div>
                </div>
                <div class="file-list" id="mlFileList">
                  <div style="padding:12px;color:var(--text3);font-size:.77rem">Loading…</div>
                </div>
              </div>
              <div style="margin-top:14px">
                <button class="btn btn-primary" id="mlTrainBtn" onclick="trainModel()" style="width:100%">🔬 Train Model</button>
              </div>

              <!-- Fetch History -->
              <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border)">
                <div style="font-size:.82rem;font-weight:700;margin-bottom:6px;color:var(--text)">📡 Fetch Historical Ticks</div>
                <div style="font-size:.75rem;color:var(--text3);margin-bottom:10px;line-height:1.5">Download tick data from Deriv API and simulate strategy outcomes → <code>data/history-trades.json</code></div>
                <div class="form-grid">
                  <div class="form-group">
                    <label>Hours of History</label>
                    <input type="number" id="fHistHours" value="48" step="12" min="1" max="720">
                    <span class="hint">48h ≈ 10K+ ticks/symbol</span>
                  </div>
                  <div class="form-group">
                    <label>App ID</label>
                    <input type="number" id="fHistAppId" value="1089" step="1" min="1">
                  </div>
                </div>
                <div style="margin-top:10px">
                  <button class="btn btn-ghost" id="fetchHistBtn" onclick="fetchHistory()" style="width:100%">📡 Fetch & Simulate</button>
                </div>
                <div class="ml-output empty" id="fetchHistOutput" style="display:none;margin-top:10px"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- RIGHT: Training output -->
        <div>
          <div class="card">
            <div class="card-header">
              <h3>Training Output</h3>
              <button class="btn btn-ghost btn-sm" onclick="refreshMlStatus()">↻ Refresh Status</button>
            </div>
            <div class="card-body" style="padding:14px">
              <div class="ml-output empty" id="mlOutput">No training run yet. Configure options and click Train Model.</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════ SCANNER TAB ═══════════════ -->
    <div class="tab-content" id="tab-scanner">
      <div class="page-header">
        <h2>Market Scanner</h2>
        <p>Real-time analysis of all indices to find optimal entry conditions</p>
      </div>

      <!-- Background Daemon -->
      <div class="daemon-box">
        <h3>🔌 Background Market Daemon</h3>
        <div class="sub">Subscribes to live tick streams and continuously updates analysis — instant results without re-fetching history. Runs independently even when the browser is closed.</div>
        <div class="form-grid" style="max-width:700px;margin-bottom:14px;grid-template-columns:1fr 1fr 1fr">
          <div class="form-group">
            <label>Snapshot Interval</label>
            <select id="fDaemonInterval">
              <option value="10">Every 10 sec</option>
              <option value="15">Every 15 sec</option>
              <option value="30" selected>Every 30 sec</option>
              <option value="60">Every 1 min</option>
            </select>
          </div>
          <div class="form-group">
            <label>Lookback Period</label>
            <select id="fDaemonHours">
              <option value="0.0167">1 min</option>
              <option value="0.0333">2 min</option>
              <option value="0.0833">5 min</option>
              <option value="0.1667">10 min</option>
              <option value="0.25">15 min</option>
              <option value="0.3333">20 min</option>
              <option value="0.5">30 min</option>
              <option value="0.75">45 min</option>
              <option value="1" selected>1 hour</option>
              <option value="2">2 hours</option>
              <option value="4">4 hours</option>
              <option value="12">12 hours</option>
              <option value="24">24 hours</option>
            </select>
          </div>
          <div class="form-group">
            <label>App ID</label>
            <input type="number" id="fDaemonAppId" value="1089" step="1" min="1">
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <button class="btn btn-teal" id="daemonStartBtn" onclick="startDaemon()">▶ Start Daemon</button>
          <button class="btn btn-danger" id="daemonStopBtn" onclick="stopDaemon()" style="display:none">⏹ Stop Daemon</button>
          <div style="display:flex;align-items:center;gap:5px">
            <div class="status-dot off" id="daemonDot"></div>
            <span style="font-size:.77rem;color:var(--text3)" id="daemonLabel">Stopped</span>
          </div>
        </div>
      </div>

      <!-- Scan Config -->
      <div class="daemon-box">
        <h3>🔍 Scan Configuration</h3>
        <div class="form-grid" style="max-width:700px;margin-bottom:14px">
          <div class="form-group">
            <label>Auto-Refresh Interval</label>
            <select id="fScanInterval">
              <option value="0">Off (manual only)</option>
              <option value="0.5">Every 30 sec</option>
              <option value="1" selected>Every 1 min</option>
              <option value="2">Every 2 min</option>
              <option value="5">Every 5 min</option>
            </select>
          </div>
          <div class="form-group">
            <label>Lookback (manual scan)</label>
            <select id="fScanHours">
              <option value="0.0167">1 min</option>
              <option value="0.0333">2 min</option>
              <option value="0.0833">5 min</option>
              <option value="0.1667">10 min</option>
              <option value="0.25">15 min</option>
              <option value="0.3333">20 min</option>
              <option value="0.5">30 min</option>
              <option value="0.75">45 min</option>
              <option value="1" selected>1 hour</option>
              <option value="2">2 hours</option>
              <option value="4">4 hours</option>
              <option value="12">12 hours</option>
              <option value="24">24 hours</option>
            </select>
          </div>
          <div class="form-group">
            <label>Scan App ID</label>
            <input type="number" id="fScanAppId" value="1089" step="1" min="1">
          </div>
        </div>
        <div style="margin-bottom:12px">
          <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:6px">Allowed Algorithms</div>
          <div class="filter-checks" id="algoFilter">
            <label class="filter-check-label"><input type="checkbox" class="algoFilterChk" value="pulse" checked> Pulse</label>
            <label class="filter-check-label"><input type="checkbox" class="algoFilterChk" value="alphabloom" checked> AlphaBloom</label>
            <label class="filter-check-label"><input type="checkbox" class="algoFilterChk" value="ensemble" checked> Ensemble</label>
            <label class="filter-check-label"><input type="checkbox" class="algoFilterChk" value="novaburst" checked> NovaBurst</label>
            <label class="filter-check-label"><input type="checkbox" class="algoFilterChk" value="adaptive" checked> Adaptive</label>
          </div>
        </div>
        <div style="margin-bottom:14px">
          <div style="font-size:.67rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:6px">Allowed Trade Strategies</div>
          <div class="filter-checks" id="tsFilter">
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="even_odd" checked> Even/Odd</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="rise_fall_roll" checked> Rise/Fall Roll</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="rise_fall_zigzag" checked> Rise/Fall Zigzag</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="higher_lower_roll"> Higher/Lower Roll</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="higher_lower_zigzag"> Higher/Lower Zigzag</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="over_under_roll"> Over/Under Roll</label>
            <label class="filter-check-label"><input type="checkbox" class="tsFilterChk" value="touch_notouch_zigzag"> Touch/NoTouch Zigzag</label>
          </div>
        </div>
        <div style="margin-bottom:14px;background:var(--surface2);border-radius:var(--radius-sm);padding:0 12px;max-width:600px">
          <div class="toggle-row">
            <div><div class="toggle-label">Auto-Exclude DO_NOT_ENTER</div><div class="toggle-sub">Automatically uncheck unsafe symbols in Bot Control</div></div>
            <label class="toggle"><input type="checkbox" id="tAutoExclude" checked><span class="toggle-slider"></span></label>
          </div>
          <div class="toggle-row">
            <div><div class="toggle-label">Auto-Apply Best Strategy</div><div class="toggle-sub">Set algorithm + strategy from top-ranked result</div></div>
            <label class="toggle"><input type="checkbox" id="tAutoStrategy"><span class="toggle-slider"></span></label>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn btn-primary" id="scanBtn" onclick="startScan()">🔍 Manual Scan</button>
          <button class="btn btn-danger" id="scanStopBtn" onclick="stopScan()" style="display:none">⏹ Stop</button>
          <button class="btn btn-ghost" onclick="refreshFromDaemon()">🔄 Refresh from Daemon</button>
          <button class="btn btn-ghost" id="autoScanBtn" onclick="toggleAutoScan()">🔁 Start Auto-Refresh</button>
          <button class="btn btn-ghost btn-sm" onclick="applyAllScanResults()">✅ Apply All to Bot</button>
          <div class="prog-wrap" id="scanProgress" style="display:none">
            <div class="spinner" style="width:14px;height:14px;border-width:2px"></div>
            <span id="scanProgressText">Connecting...</span>
            <div class="prog-bar"><div class="prog-fill" id="scanProgressFill" style="width:0%"></div></div>
          </div>
        </div>
      </div>

      <!-- Results -->
      <div id="scanResultsWrap" style="display:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
          <h3 style="font-size:.82rem;font-weight:700;color:var(--text)">Scan Results <span id="scanResultCount" style="color:var(--text3);font-weight:400"></span></h3>
          <div style="font-size:.7rem;color:var(--text3);font-family:var(--mono)" id="scanTimestamp"></div>
        </div>
        <div class="scan-results" id="scanResults"></div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
// ─── STATE ───────────────────────────────────────────────────────────────────
let sessions = [], activeFile = null, activeData = null;
let charts = {}, autoTimer = null, botRunning = false;
let currentMode = 'demo';
let logSSE = null; // SSE connection for bot logs

// ─── TABS ─────────────────────────────────────────────────────────────────────
const TAB_TITLES = { analytics: 'Session Analytics', control: 'Bot Control', training: 'ML Training', scanner: 'Market Scanner' };
function switchTab(tab, el) {
  document.querySelectorAll('.sidebar-item').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tab));
  document.getElementById('topbarTitle').textContent = TAB_TITLES[tab] || tab;
  if (tab === 'control') { startLogSSE(); }
  else { stopLogSSE(); }
  if (tab === 'training') { refreshMlStatus(); loadMlFiles(); }
}

// ─── SSE BOT LOGS ─────────────────────────────────────────────────────────────
let logPollTimer = null;

function startLogSSE() {
  stopLogSSE();
  const dot = document.getElementById('sseDot');
  const statusEl = document.getElementById('sseStatus');
  dot.className = 'sse-dot'; statusEl.textContent = 'Live (Polling)';
  
  async function poll() {
    try {
      const d = await apiFetch('?api=bot_status');
      updateBotUI(d.running, d.logs);
      dot.className = 'sse-dot'; statusEl.textContent = 'Live';
    } catch(e) {
      dot.className = 'sse-dot off'; statusEl.textContent = 'Error fetching logs';
    }
  }
  
  poll();
  logPollTimer = setInterval(poll, 1500);
}

function stopLogSSE() {
  if (logPollTimer) { clearInterval(logPollTimer); logPollTimer = null; }
  const dot = document.getElementById('sseDot');
  const statusEl = document.getElementById('sseStatus');
  if (dot) dot.className = 'sse-dot off';
  if (statusEl) statusEl.textContent = 'Disconnected';
}

function reconnectSSE() { startLogSSE(); }

function updateBotUI(running, logs) {
  botRunning = running;
  const pill = document.getElementById('statusPill');
  const dot = document.getElementById('statusDot');
  const lbl = document.getElementById('statusLabel');
  const stopBtn = document.getElementById('stopBtn');
  const sidebarDot = document.getElementById('sidebarBotDot');
  const topDot = document.getElementById('topBotDot');
  const sfBotStatus = document.getElementById('sfBotStatus');
  if (running) {
    pill.className = 'status-pill running';
    dot.className = 'status-dot on';
    lbl.textContent = 'Running';
    stopBtn.disabled = false;
    sidebarDot.className = 'si-dot on';
    topDot.className = 'dot on';
    if (sfBotStatus) { sfBotStatus.textContent = 'running'; sfBotStatus.style.color = '#68d391'; }
  } else {
    pill.className = 'status-pill stopped';
    dot.className = 'status-dot off';
    lbl.textContent = 'Stopped';
    stopBtn.disabled = true;
    sidebarDot.className = 'si-dot off';
    topDot.className = 'dot off';
    if (sfBotStatus) { sfBotStatus.textContent = 'stopped'; sfBotStatus.style.color = '#fc8181'; }
  }
  const logEl = document.getElementById('logOutput');
  if (logs && logs.trim()) {
    logEl.className = 'log-output';
    logEl.textContent = logs;
    logEl.scrollTop = logEl.scrollHeight;
  } else {
    logEl.className = 'log-output empty';
    logEl.textContent = running ? 'Waiting for output...' : 'Bot is not running.';
  }
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

// ─── BACKGROUND STATUS POLLING ─────────────────────────────────────────────────
async function pollBgStatus() {
  try {
    const d = await apiFetch('?api=bg_status');
    // Bot indicators
    const topBotDot = document.getElementById('topBotDot');
    const sidebarBotDot = document.getElementById('sidebarBotDot');
    const sfBotStatus = document.getElementById('sfBotStatus');
    if (topBotDot) topBotDot.className = d.bot_running ? 'dot on' : 'dot off';
    if (sidebarBotDot) sidebarBotDot.className = d.bot_running ? 'si-dot on' : 'si-dot off';
    if (sfBotStatus) { sfBotStatus.textContent = d.bot_running ? 'running' : 'stopped'; sfBotStatus.style.color = d.bot_running ? '#68d391' : '#fc8181'; }
    botRunning = d.bot_running;
    // Daemon indicators
    const topDaemonDot = document.getElementById('topDaemonDot');
    const sidebarDaemonDot = document.getElementById('sidebarDaemonDot');
    const sfDaemonStatus = document.getElementById('sfDaemonStatus');
    if (topDaemonDot) topDaemonDot.className = d.daemon_running ? 'dot on' : 'dot off';
    if (sidebarDaemonDot) sidebarDaemonDot.className = d.daemon_running ? 'si-dot on' : 'si-dot off';
    if (sfDaemonStatus) { sfDaemonStatus.textContent = d.daemon_running ? `running (${d.daemon_file_age}s)` : 'stopped'; sfDaemonStatus.style.color = d.daemon_running ? '#68d391' : '#fc8181'; }
  } catch(e) {}
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
let sessionsOffset = 0;
let sessionsLimit = 10;
let loadingSessions = false;
let allSessionsLoaded = false;

async function init() {
  updateCmd();
  
  const grid = document.getElementById('sessionGrid');
  grid.addEventListener('scroll', function() {
    if (this.scrollLeft + this.clientWidth >= this.scrollWidth - 100) {
      if (!loadingSessions && !allSessionsLoaded) loadSessions(true);
    }
  });

  await loadSessions();
  pollBgStatus();
  setInterval(pollBgStatus, 8000); // background polling every 8s
}

async function loadSessions(append = false, silent = false) {
  if (loadingSessions) return;
  loadingSessions = true;
  try {
    if (!append && !silent) {
      sessionsOffset = 0;
      allSessionsLoaded = false;
      document.getElementById('sessionGrid').innerHTML = '<div style="padding:20px;color:var(--text3);display:flex;align-items:center"><div class="spinner" style="margin-right:8px;border-width:2px;width:14px;height:14px"></div> Loading sessions...</div>';
    } else if (!append && silent) {
      sessionsOffset = 0;
      allSessionsLoaded = false;
    }
    
    const newSessions = await apiFetch(`?api=sessions&offset=${sessionsOffset}&limit=${sessionsLimit}`);
    
    if (newSessions.length < sessionsLimit) allSessionsLoaded = true;
    
    if (!append) sessions = newSessions;
    else sessions = sessions.concat(newSessions);
    
    sessionsOffset += newSessions.length;
    
    renderSessionGrid(append, newSessions, silent);
    document.getElementById('sessCount').textContent = sessions.length;
    
    if (!append && activeFile && sessions.find(s => s.file === activeFile)) {
      await loadSession(activeFile, false, silent);
    }
  } catch(e) { console.error('Sessions:', e); }
  loadingSessions = false;
}

function refreshAll() {
  loadSessions(false, true);
  pollBgStatus();
}

document.getElementById('autoRefresh').addEventListener('change', function() {
  clearInterval(autoTimer);
  if (this.checked) autoTimer = setInterval(refreshAll, 10000);
});

// ─── SESSION GRID ─────────────────────────────────────────────────────────────
function renderSessionGrid(append = false, newSessions = null, silent = false) {
  const grid = document.getElementById('sessionGrid');
  if (!sessions.length) {
    grid.innerHTML = '<div style="color:var(--text3);padding:20px;font-size:.85rem">No sessions found in data/ folder.</div>';
    return;
  }
  const toRender = append ? (newSessions || []) : sessions;
  const html = toRender.map(s => {
    const pnl = s.net_pnl ?? 0;
    const pnlClass = pnl >= 0 ? 'c-green' : 'c-red';
    const wr = ((s.win_rate ?? 0) * 100).toFixed(1);
    const modeBadge = s.account_mode === 'live'
      ? '<span class="sc-mode live">Live</span>'
      : '<span class="sc-mode demo">Demo</span>';
    return `<div class="session-card ${s.is_live?'live-card':''} ${activeFile===s.file?'active-card':''}"
         data-file="${s.file}" onclick="loadSession('${s.file}')">
      ${modeBadge}
      <div class="sc-id">${s.file.replace('.json','')}</div>
      <div class="sc-date">${fmtTs(s.started_at)}</div>
      <div class="sc-stats">
        <div class="sc-stat"><span class="k">Trades</span><span class="v">${s.trade_count}</span></div>
        <div class="sc-stat"><span class="k">Win Rate</span><span class="v">${wr}%</span></div>
        <div class="sc-stat"><span class="k">W / L</span><span class="v">${s.wins} / ${s.losses}</span></div>
        <div class="sc-stat"><span class="k">P&amp;L</span><span class="v ${pnlClass}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span></div>
      </div>
    </div>`;
  }).join('');
  
  if (append) {
    grid.insertAdjacentHTML('beforeend', html);
  } else {
    const scrollLeft = grid.scrollLeft;
    grid.innerHTML = html;
    if (silent) grid.scrollLeft = scrollLeft;
  }
}

// ─── LOAD SESSION ─────────────────────────────────────────────────────────────
async function loadSession(file, scroll=true) {
  activeFile = file;
  
  // Highlight card immediately
  document.querySelectorAll('.session-card').forEach(c =>
    c.classList.toggle('active-card', c.dataset.file === file));
    
  document.getElementById('emptyState').style.display = 'none';
  const dash = document.getElementById('dashboard');
  dash.style.display = 'block';
  dash.style.opacity = '0.5';
  dash.style.pointerEvents = 'none';
  
  // Optional: Add a simple loading indicator at the top
  const headerText = document.getElementById('topbarTitle');
  const oldText = headerText.textContent;
  headerText.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-width:2px;margin-right:8px"></div>Loading Session Data...';

  try {
    // Fresh read — never use cached data
    activeData = await apiFetch('?api=session&file=' + encodeURIComponent(file) + '&_t=' + Date.now());
    renderDashboard();
    if (scroll) document.getElementById('dashboard').scrollIntoView({ behavior:'smooth', block:'start' });
  } catch(e) { console.error('Session load:', e); }
  
  dash.style.opacity = '1';
  dash.style.pointerEvents = 'auto';
  headerText.textContent = oldText;
}

// ─── RENDER DASHBOARD ─────────────────────────────────────────────────────────
function renderDashboard(silent = false) {
  if (!activeData) return;
  const d = activeData, sess = d.session||{}, sum = d.summary||{};
  const trades = d.trades||[], curve = d.equity_curve||[];

  // Config bar
  document.getElementById('configBar').innerHTML = `
    <div class="ci-item"><div class="k">Account</div><div class="v">${sess.account_loginid||'—'}</div></div>
    <div class="ci-item"><div class="k">Mode</div><div class="v"><span class="badge-mode-${sess.account_mode==='live'?'live':'demo'}">${sess.account_mode||'demo'}</span></div></div>
    <div class="ci-item"><div class="k">Base Stake</div><div class="v">$${sess.base_stake??'—'}</div></div>
    <div class="ci-item"><div class="k">Score Thr.</div><div class="v">${sess.score_threshold??'—'}</div></div>
    <div class="ci-item"><div class="k">Profit Target</div><div class="v" style="color:var(--green-light)">${sess.profit_target!=null?'$'+sess.profit_target:'∞'}</div></div>
    <div class="ci-item"><div class="k">Loss Limit</div><div class="v" style="color:var(--red-light)">${sess.loss_limit!=null?'$'+sess.loss_limit:'∞'}</div></div>
    <div class="ci-item"><div class="k">Duration</div><div class="v">${computeDuration(sess.started_at,sess.updated_at)}</div></div>
    <div class="ci-item" style="flex:2"><div class="k">Symbols</div><div class="v"><div class="config-syms">${(sess.symbols||[]).map(s=>`<span class="sym-tag">${s}</span>`).join('')}</div></div></div>
  `;

  // Compute stats from trades array (source of truth — avoids summary/trade mismatch)
  const initEq = sess.initial_equity||0;
  const tradeWins = trades.filter(t=>t.result==='win');
  const tradeLosses = trades.filter(t=>t.result==='loss');
  const totalTrades = trades.length;
  const netPnl = trades.reduce((s,t)=>s+t.profit,0);
  const curEq = totalTrades>0 ? trades[trades.length-1].equity_after : initEq;
  const wr = totalTrades>0 ? (tradeWins.length/totalTrades*100) : 0;

  let runPeak = initEq, maxDD = 0, runEquity = initEq;
  for (const t of trades) {
    runEquity = t.equity_after;
    if (runEquity > runPeak) runPeak = runEquity;
    const dd = runPeak - runEquity;
    if (dd > maxDD) maxDD = dd;
  }

  let maxWinStreak = 0, maxWinPnl = 0, maxLossStreak = 0, maxLossPnl = 0;
  if (trades.length > 0) {
    let curType = trades[0].result, curLen = 1, curPnl = trades[0].profit;
    for (let i = 1; i <= trades.length; i++) {
      if (i < trades.length && trades[i].result === curType) {
        curLen++; curPnl += trades[i].profit;
      } else {
        if (curType === 'win') {
          if (curLen > maxWinStreak || (curLen === maxWinStreak && curPnl > maxWinPnl)) { maxWinStreak = curLen; maxWinPnl = curPnl; }
        } else {
          if (curLen > maxLossStreak || (curLen === maxLossStreak && curPnl < maxLossPnl)) { maxLossStreak = curLen; maxLossPnl = curPnl; }
        }
        if (i < trades.length) { curType = trades[i].result; curLen = 1; curPnl = trades[i].profit; }
      }
    }
  }

  const eqChange = curEq - initEq;
  const pnlColor = netPnl>=0?'var(--green-light)':'var(--red-light)';
  const eqColor = eqChange>=0?'var(--green-light)':'var(--red-light)';

  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card ${netPnl>=0?'green':'red'}">
      <div class="label">Net P&amp;L</div>
      <div class="value" style="color:${pnlColor}">${netPnl>=0?'+':''}$${netPnl.toFixed(2)}</div>
      <div class="sub">${totalTrades} trades total</div>
      <div class="icon">${netPnl>=0?'📈':'📉'}</div>
    </div>
    <div class="stat-card blue">
      <div class="label">Win Rate</div>
      <div class="value">${wr.toFixed(1)}%</div>
      <div class="sub">${tradeWins.length}W / ${tradeLosses.length}L</div>
      <div class="icon">🎯</div>
    </div>
    <div class="stat-card teal">
      <div class="label">Equity</div>
      <div class="value">$${curEq.toFixed(2)}</div>
      <div class="sub" style="color:${eqColor}">${eqChange>=0?'+':''}$${eqChange.toFixed(2)}</div>
      <div class="icon">💰</div>
    </div>
    <div class="stat-card green">
      <div class="label">Peak Equity</div>
      <div class="value">$${runPeak.toFixed(2)}</div>
      <div class="sub">+$${(runPeak-initEq).toFixed(2)} from start</div>
      <div class="icon">🏆</div>
    </div>
    <div class="stat-card red">
      <div class="label">Max Drawdown</div>
      <div class="value">-$${maxDD.toFixed(2)}</div>
      <div class="sub">${initEq>0?((maxDD/initEq)*100).toFixed(1):0}% of initial</div>
      <div class="icon">⚠️</div>
    </div>
    <div class="stat-card purple">
      <div class="label">Best / Worst Streak</div>
      <div class="value" style="font-size:1rem">${maxWinStreak}W / ${maxLossStreak}L</div>
      <div class="sub"><span style="color:var(--green-light)">+$${maxWinPnl.toFixed(2)}</span> <span style="color:var(--text4)">|</span> <span style="color:var(--red-light)">-$${Math.abs(maxLossPnl).toFixed(2)}</span></div>
      <div class="icon">⚡</div>
    </div>
  `;

  // Chart sub text
  document.getElementById('eqChartSub').textContent = `Initial: $${initEq.toFixed(2)} → Current: $${curEq.toFixed(2)}`;

  renderCharts(trades, curve, initEq, silent);
  if (!silent) renderStreaks(trades);
  if (!silent) renderSymbolBreakdown(trades);

  // Trade log — compute cum P&L live from trades
  document.getElementById('tradeCount').textContent = `${trades.length} trades`;
  let cum = 0;
  
  const tradeWrap = document.getElementById('tradeBody').parentElement.parentElement;
  const oldScroll = tradeWrap.scrollTop;
  
  document.getElementById('tradeBody').innerHTML = trades.map(t => {
    cum += t.profit;
    const win = t.result==='win';
    const ps = t.profit>=0?'+':'', cs = cum>=0?'+':'';
    return `<tr>
      <td>${t.trade_no}</td>
      <td style="font-size:.7rem;color:var(--text3)">${fmtTs(t.timestamp)}</td>
      <td><span class="sym-tag">${t.symbol}</span></td>
      <td>${contractBadge(t.contract_type)}</td>
      <td><span class="badge ${win?'badge-win':'badge-loss'}">${t.result.toUpperCase()}</span></td>
      <td>$${t.stake.toFixed(2)}</td>
      <td style="color:${t.profit>=0?'var(--green-light)':'var(--red-light)'};font-weight:600">${ps}$${t.profit.toFixed(2)}</td>
      <td>$${t.payout.toFixed(2)}</td>
      <td style="color:${cum>=0?'var(--green-light)':'var(--red-light)'}">${cs}$${cum.toFixed(2)}</td>
      <td>$${t.equity_after.toFixed(2)}</td>
    </tr>`;
  }).join('');
}

// ─── CHARTS ──────────────────────────────────────────────────────────────────
function destroyCharts(){ Object.values(charts).forEach(c=>c.destroy()); charts={}; }

const CHART_FONT = 'Plus Jakarta Sans';
const TICK_COLOR = '#718096';
const GRID_COLOR = '#e2e8f0';

function baseOpts(height) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400, easing: 'easeOutQuart' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#1a202c',
        titleColor: '#e2e8f0',
        bodyColor: '#a0aec0',
        borderColor: '#2d3748',
        borderWidth: 1,
        titleFont: { family: 'IBM Plex Mono', size: 11 },
        bodyFont: { family: 'IBM Plex Mono', size: 11 },
        padding: 10,
      }
    },
    scales: {
      x: {
        grid: { color: GRID_COLOR, drawBorder: false },
        ticks: { color: TICK_COLOR, font: { family: CHART_FONT, size: 10 }, maxTicksLimit: 12 }
      },
      y: {
        grid: { color: GRID_COLOR, drawBorder: false },
        ticks: { color: TICK_COLOR, font: { family: CHART_FONT, size: 10 }, callback: v => '$' + v.toFixed(2) }
      }
    }
  };
}

function renderCharts(trades, curve, initEq, silent = false) {
  if (!silent) destroyCharts();
  if (!trades.length) return;

  // Use equity_after from trades directly — most reliable source
  const labels = trades.map(t => '#' + t.trade_no);
  const eqVals = trades.map(t => t.equity_after);
  let cum = 0;
  const pnlVals = trades.map(t => { cum += t.profit; return cum; });
  const pointColors = trades.map(t => t.result==='win' ? '#38a169' : '#e53e3e');

  const pnlFinal = pnlVals[pnlVals.length-1] ?? 0;

  if (silent && charts.eq) {
    charts.eq.data.labels = labels;
    charts.eq.data.datasets[0].data = eqVals;
    charts.eq.data.datasets[1].data = pnlVals;
    charts.eq.update('none');
  } else {
    // Equity chart — combined equity + PnL on dual axis
    const eqCtx = document.getElementById('equityChart').getContext('2d');
    const eqGrad = eqCtx.createLinearGradient(0,0,0,200);
    eqGrad.addColorStop(0,'rgba(49,130,206,0.15)');
    eqGrad.addColorStop(1,'rgba(49,130,206,0.01)');
    const pnlGrad = eqCtx.createLinearGradient(0,0,0,200);
    pnlGrad.addColorStop(0, pnlFinal>=0?'rgba(56,161,105,0.18)':'rgba(229,62,62,0.15)');
    pnlGrad.addColorStop(1,'transparent');

    charts.eq = new Chart(eqCtx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Equity',
            data: eqVals,
            borderColor: '#3182ce',
            backgroundColor: eqGrad,
            fill: true,
            tension: 0.35,
            pointRadius: trades.length > 80 ? 0 : 2,
            borderWidth: 2,
            yAxisID: 'y',
          },
          {
            label: 'Cum P&L',
            data: pnlVals,
            borderColor: pnlFinal>=0?'#38a169':'#e53e3e',
            backgroundColor: pnlGrad,
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            borderWidth: 1.5,
            borderDash: [4,3],
            yAxisID: 'y2',
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        animation: { duration: 400 },
        plugins: {
          legend: { display: true, position: 'top', labels: { color: TICK_COLOR, font: { family: CHART_FONT, size: 11 }, boxWidth: 12, padding: 12 } },
          tooltip: { backgroundColor:'#1a202c',titleColor:'#e2e8f0',bodyColor:'#a0aec0',borderColor:'#2d3748',borderWidth:1,titleFont:{family:'IBM Plex Mono',size:11},bodyFont:{family:'IBM Plex Mono',size:11},padding:10 }
        },
        scales: {
          x: { grid:{color:GRID_COLOR,drawBorder:false}, ticks:{color:TICK_COLOR,font:{family:CHART_FONT,size:10},maxTicksLimit:10} },
          y: { position:'left', grid:{color:GRID_COLOR,drawBorder:false}, ticks:{color:'#3182ce',font:{family:CHART_FONT,size:10},callback:v=>'$'+v.toFixed(2)} },
          y2: { position:'right', grid:{display:false}, ticks:{color:pnlFinal>=0?'#38a169':'#e53e3e',font:{family:CHART_FONT,size:10},callback:v=>'$'+v.toFixed(2)} }
        }
      }
    });
  }

  // Win/Loss donut
  const wins = trades.filter(t=>t.result==='win').length;
  if (silent && charts.wl) {
    charts.wl.data.datasets[0].data = [wins, trades.length - wins];
    charts.wl.update('none');
  } else {
    charts.wl = new Chart(document.getElementById('wlChart').getContext('2d'), {
      type: 'doughnut',
      data: {
        labels: ['Wins','Losses'],
        datasets: [{ data:[wins,trades.length-wins], backgroundColor:['rgba(56,161,105,0.8)','rgba(229,62,62,0.8)'], borderColor:['#38a169','#e53e3e'], borderWidth:2 }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '70%',
        animation: { duration: 600 },
        plugins: {
          legend: { position: 'bottom', labels: { color: TICK_COLOR, font: { family: CHART_FONT, size: 12 }, padding: 16 } },
          tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${ctx.raw} (${((ctx.raw/trades.length)*100).toFixed(1)}%)` } }
        }
      }
    });
  }

  // Stake progression
  const stakeVals = trades.map(t=>t.stake);
  if (silent && charts.stake) {
    charts.stake.data.labels = labels;
    charts.stake.data.datasets[0].data = stakeVals;
    charts.stake.data.datasets[0].backgroundColor = pointColors.map(c=>c+'90');
    charts.stake.data.datasets[0].borderColor = pointColors;
    charts.stake.update('none');
  } else {
    charts.stake = new Chart(document.getElementById('stakeChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels,
        datasets: [{ data: stakeVals, backgroundColor: pointColors.map(c=>c+'90'), borderColor: pointColors, borderWidth:1, borderRadius:2 }]
      },
      options: {
        ...baseOpts(160),
        animation: { duration: 300 }
      }
    });
  }
}

// ─── STREAKS ─────────────────────────────────────────────────────────────────
function computeStreaks(trades) {
  if (!trades.length) return [];
  const out = []; let cur = { type: trades[0].result, start: 0, length: 1, pnl: trades[0].profit };
  for (let i = 1; i < trades.length; i++) {
    if (trades[i].result === cur.type) { cur.length++; cur.pnl += trades[i].profit; }
    else { cur.end = i-1; out.push({...cur}); cur = { type: trades[i].result, start: i, length: 1, pnl: trades[i].profit }; }
  }
  cur.end = trades.length-1; out.push(cur);
  return out;
}

function renderStreaks(trades) {
  const streaks = computeStreaks(trades);
  const total = trades.length || 1;
  document.getElementById('streakBar').innerHTML = streaks.map(sk => {
    const pct = (sk.length/total)*100;
    const sign = sk.pnl>=0?'+':'';
    return `<div class="streak-seg ${sk.type}" style="width:${Math.max(pct,0.8)}%">${sk.length>2?sk.length:''}
      <div class="streak-tip">${sk.type.toUpperCase()} ×${sk.length}<br>${sign}$${sk.pnl.toFixed(2)}<br>#${sk.start+1}–#${sk.end+1}</div>
    </div>`;
  }).join('');
  const notable = streaks.filter(s=>s.length>=2).sort((a,b)=>b.length-a.length).slice(0,8);
  document.getElementById('streakBody').innerHTML = notable.length
    ? notable.map(sk=>{
        const sign = sk.pnl>=0?'+':'';
        return `<tr><td><span class="badge ${sk.type==='win'?'badge-win':'badge-loss'}">${sk.type.toUpperCase()}</span></td><td>${sk.length} in a row</td><td>#${sk.start+1}–#${sk.end+1}</td><td style="color:${sk.pnl>=0?'var(--green-light)':'var(--red-light)'}">${sign}$${sk.pnl.toFixed(2)}</td></tr>`;
      }).join('')
    : '<tr><td colspan="4" style="color:var(--text3);padding:12px">No streaks of 2+ yet</td></tr>';
}

// ─── SYMBOL BREAKDOWN ─────────────────────────────────────────────────────────
function renderSymbolBreakdown(trades) {
  const map = {};
  for (const t of trades) {
    if (!map[t.symbol]) map[t.symbol] = { wins:0, losses:0, pnl:0 };
    if (t.result==='win') map[t.symbol].wins++; else map[t.symbol].losses++;
    map[t.symbol].pnl += t.profit;
  }
  document.getElementById('symbolGrid').innerHTML = Object.entries(map)
    .sort((a,b)=>(b[1].wins+b[1].losses)-(a[1].wins+a[1].losses))
    .map(([sym,s]) => {
      const tot = s.wins+s.losses, wr = tot ? (s.wins/tot*100):0;
      const sign = s.pnl>=0?'+':'', pnlColor = s.pnl>=0?'var(--green-light)':'var(--red-light)';
      const barColor = wr>=50?'var(--green-light)':'var(--red-light)';
      return `<div class="sym-card">
        <div class="sym-name">${sym}</div>
        <div class="sym-row">
          <div class="sym-stat"><span class="k">Trades</span><span class="v">${tot}</span></div>
          <div class="sym-stat"><span class="k">W/L</span><span class="v">${s.wins}/${s.losses}</span></div>
          <div class="sym-stat"><span class="k">Win%</span><span class="v">${wr.toFixed(0)}%</span></div>
          <div class="sym-stat"><span class="k">P&L</span><span class="v" style="color:${pnlColor}">${sign}$${s.pnl.toFixed(2)}</span></div>
        </div>
        <div class="sym-bar"><div class="sym-bar-fill" style="width:${wr}%;background:${barColor}"></div></div>
      </div>`;
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
  const isAdaptive = s === 'adaptive';
  document.getElementById('abWindowGroup').style.display = s==='alphabloom'?'':'none';
  document.getElementById('hotnessColdGroup').style.display = isAdaptive?'':'none';
  document.getElementById('hotnessProbeGroup').style.display = isAdaptive?'':'none';
  document.getElementById('mlIdleGroup').style.display = isAdaptive?'':'none';
  document.getElementById('mlFloorGroup').style.display = isAdaptive?'':'none';
  document.getElementById('volSkipGroup').style.display = isAdaptive?'':'none';
  syncMlThresholdVisibility();
}

function syncMlThresholdVisibility() {
  const s = document.getElementById('fStrategy').value;
  const mlOn = document.getElementById('tMl').checked;
  document.getElementById('mlThresholdGroup').style.display = (s==='adaptive'||mlOn)?'':'none';
}

function contractBadge(ct) {
  if (!ct) return '<span class="badge">—</span>';
  if (ct.includes('ODD'))   return '<span class="badge badge-odd">ODD</span>';
  if (ct.includes('EVEN'))  return '<span class="badge badge-even">EVEN</span>';
  if (ct==='CALL')          return '<span class="badge badge-call">CALL ↑</span>';
  if (ct==='PUT')           return '<span class="badge badge-put">PUT ↓</span>';
  if (ct==='DIGITOVER')     return '<span class="badge badge-over">OVER</span>';
  if (ct==='DIGITUNDER')    return '<span class="badge badge-under">UNDER</span>';
  if (ct==='ONETOUCH')      return '<span class="badge badge-touch">TOUCH</span>';
  if (ct==='NOTOUCH')       return '<span class="badge badge-notouch">NO TOUCH</span>';
  return `<span class="badge">${ct}</span>`;
}

function buildParams() {
  return {
    token:          document.getElementById('fToken').value.trim() || 'gY5gbEpJVhih5NL',
    mode:           currentMode,
    base_stake:     parseFloat(document.getElementById('fStake').value)||0.35,
    martingale:     parseFloat(document.getElementById('fMartingale').value)||2.2,
    max_stake:      parseFloat(document.getElementById('fMaxStake').value)||50,
    threshold:      parseFloat(document.getElementById('fThreshold').value)||0.60,
    strategy:       document.getElementById('fStrategy').value,
    trade_strategy: document.getElementById('fTradeStrategy').value,
    ab_window:      parseInt(document.getElementById('fAbWindow').value)||60,
    disable_kelly:  document.getElementById('tKelly').checked,
    disable_risk:   document.getElementById('tRisk').checked,
    ml_filter:      document.getElementById('tMl').checked,
    ml_threshold:   (() => { const v = parseFloat(document.getElementById('fMlThreshold').value); return isNaN(v)?null:v; })(),
    hotness_cold:   parseFloat(document.getElementById('fHotnessCold').value)||0.43,
    hotness_probe:  parseInt(document.getElementById('fHotnessProbe').value)||20,
    ml_idle_minutes:parseFloat(document.getElementById('fMlIdle').value)||10,
    ml_floor:       parseFloat(document.getElementById('fMlFloor').value)||0.35,
    vol_skip_pct:   parseFloat(document.getElementById('fVolSkip').value)||0.75,
    profit_target:  (() => { const v = document.getElementById('fProfit').value.trim(); return v?parseFloat(v):null; })(),
    loss_limit:     (() => { const v = document.getElementById('fLoss').value.trim(); return v?parseFloat(v):null; })(),
    symbols:        getSelectedSymbols(),
  };
}

function updateCmd() {
  const p = buildParams();
  const hintLevels = p.martingale > 1 ? Math.floor(Math.log(p.max_stake/p.base_stake)/Math.log(p.martingale)) : '∞';
  const hintEl = document.getElementById('hMaxStake');
  if (hintEl) hintEl.textContent = `Covers up to ${hintLevels} consecutive losses`;
  syncMlThresholdVisibility();
  const tsHints = { even_odd:'DIGITEVEN/DIGITODD — 5 ticks', rise_fall_roll:'CALL/PUT + Roll Cake — 5 ticks', rise_fall_zigzag:'CALL/PUT + Zigzag — 7 ticks', higher_lower_roll:'CALL/PUT + barrier + Roll — 5 ticks', higher_lower_zigzag:'CALL/PUT + barrier + Zigzag — 7 ticks', over_under_roll:'DIGITOVER/UNDER + Roll — 5 ticks', touch_notouch_zigzag:'TOUCH/NOTOUCH + Zigzag — 7 ticks' };
  const hintEl2 = document.getElementById('hTradeStrategy');
  if (hintEl2) hintEl2.textContent = tsHints[p.trade_strategy]||'Contract type + pattern';
  const tokenDisplay = p.token.length>5 ? p.token.slice(0,3)+'***'+p.token.slice(-2) : p.token;
  let cmd = `python3 bot.py --token <span class="cmd-hl">${tokenDisplay}</span> --account-mode ${p.mode}`;
  cmd += ` --base-stake ${p.base_stake.toFixed(2)} --martingale ${p.martingale.toFixed(1)} --max-stake ${p.max_stake.toFixed(0)}`;
  cmd += ` --score-threshold ${p.threshold.toFixed(2)} --strategy ${p.strategy} --trade-strategy ${p.trade_strategy}`;
  if (p.strategy==='alphabloom') cmd += ` --ab-window ${p.ab_window}`;
  if (p.disable_kelly) cmd += ' --disable-kelly';
  if (p.disable_risk)  cmd += ' --disable-risk-engine';
  if (p.ml_filter && p.strategy!=='adaptive') cmd += ' --ml-filter';
  if ((p.ml_filter||p.strategy==='adaptive') && p.ml_threshold!==null) cmd += ` --ml-threshold ${p.ml_threshold.toFixed(2)}`;
  if (p.strategy==='adaptive') cmd += ` --hotness-cold ${p.hotness_cold.toFixed(2)} --hotness-probe ${p.hotness_probe} --vol-skip-pct ${p.vol_skip_pct.toFixed(2)} --ml-idle-minutes ${p.ml_idle_minutes.toFixed(0)} --ml-floor ${p.ml_floor.toFixed(2)}`;
  if (p.profit_target!==null) cmd += ` --profit-target ${p.profit_target}`;
  if (p.loss_limit!==null)    cmd += ` --loss-limit ${p.loss_limit}`;
  if (p.symbols && p.symbols.length>0 && p.symbols.length<ALL_SYMBOLS.length) cmd += ` --symbols ${p.symbols.join(' ')}`;
  document.getElementById('cmdPreview').innerHTML = cmd;
}

async function startBot() {
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="margin-right:6px"></div> Starting...';
  try {
    const res = await apiPost('?api=bot_start', buildParams());
    if (res.success) {
      updateBotUI(true, 'Bot starting...\n');
      setTimeout(() => { loadSessions(false); pollBgStatus(); }, 2500);
    } else {
      alert('Failed to start bot.\nReturn: ' + res.ret + '\n' + (res.out||''));
    }
  } catch(e) { alert('Error: ' + e.message); }
  btn.disabled = false;
  btn.innerHTML = '🚀 Start Bot';
}

async function stopBot() {
  if (!confirm('Send Ctrl+C and kill the bbot tmux session?')) return;
  const btn = document.getElementById('stopBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div>';
  try {
    const res = await apiPost('?api=bot_stop', {});
    if (res.success) {
      updateBotUI(false, '');
    }
    setTimeout(() => { loadSessions(false); pollBgStatus(); btn.innerHTML='⏹ Stop'; }, 2500);
  } catch(e) { btn.disabled=false; btn.innerHTML='⏹ Stop'; }
}

// ─── SYMBOL CHECKLIST ─────────────────────────────────────────────────────────
const ALL_SYMBOLS = ['R_10','R_25','R_50','R_75','R_100','1HZ10V','1HZ25V','1HZ50V','1HZ75V','1HZ100V'];

function initSymChecklist() {
  const wrap = document.getElementById('symChecklist');
  wrap.innerHTML = ALL_SYMBOLS.map(s => `
    <label style="display:flex;align-items:center;gap:4px;padding:4px 10px;background:var(--surface2);border:1.5px solid var(--border);border-radius:20px;cursor:pointer;font-family:var(--mono);font-size:.73rem;font-weight:600;color:var(--text2);transition:all .12s">
      <input type="checkbox" id="sym_${s}" value="${s}" checked onchange="updateSymCount();updateCmd()" style="accent-color:var(--blue-light)">
      ${s}
    </label>`).join('');
  updateSymCount();
}

function getSelectedSymbols() {
  return ALL_SYMBOLS.filter(s => { const c = document.getElementById('sym_'+s); return c && c.checked; });
}
function updateSymCount() {
  const el = document.getElementById('symSelectedCount');
  if (el) el.textContent = `(${getSelectedSymbols().length}/${ALL_SYMBOLS.length})`;
}
function symSelectAll(checked) {
  ALL_SYMBOLS.forEach(s => { const c = document.getElementById('sym_'+s); if(c) c.checked=checked; });
  updateSymCount(); updateCmd();
}

// ─── ML TRAINING ─────────────────────────────────────────────────────────────
let mlFiles = [];

async function refreshMlStatus() {
  const statusEl = document.getElementById('mlStatus');
  const gridEl   = document.getElementById('mlMetaGrid');
  const thrWrap  = document.getElementById('mlThresholdsWrap');
  const thrTable = document.getElementById('mlThrTable');
  const dot      = document.getElementById('sidebarMlDot');
  try {
    const s = await apiFetch('?api=ml_model_status');
    if (!s.trained) {
      statusEl.innerHTML = `<div class="ml-dot untrained"></div><div style="font-size:.8rem;color:var(--text)">Not trained <span style="color:var(--text3)">— no data/ml_filter.pkl yet</span></div>`;
      gridEl.style.display='none'; thrWrap.style.display='none';
      dot.className='si-dot off'; return;
    }
    dot.className='si-dot on';
    const when = s.pkl_mtime ? fmtTs(s.pkl_mtime) : '—';
    statusEl.innerHTML = `<div class="ml-dot trained"></div><div style="font-size:.8rem;color:var(--text)">Trained <span style="color:var(--text3)">— updated ${when}</span></div>`;
    const m = s.meta||{};
    if (Object.keys(m).length) {
      const teAuc = m.test?.auc??null, trAuc = m.train?.auc??null;
      const aucColor = teAuc!=null && teAuc>0.55?'var(--green-light)':teAuc!=null&&teAuc>0.52?'var(--amber)':'var(--red-light)';
      gridEl.innerHTML = `
        <div class="ml-meta-item"><div class="lbl">Model</div><div class="val">${m.model_kind||'—'}</div></div>
        <div class="ml-meta-item"><div class="lbl">Threshold</div><div class="val">${(m.threshold??0).toFixed(2)}</div></div>
        <div class="ml-meta-item"><div class="lbl">Session Trades</div><div class="val">${m.n_session_trades||0}</div></div>
        <div class="ml-meta-item"><div class="lbl">History Trades</div><div class="val" style="color:${(m.n_history_trades||0)>0?'var(--teal)':'var(--text3)'}">${m.n_history_trades||0}</div></div>
        <div class="ml-meta-item"><div class="lbl">Train / Test</div><div class="val">${m.n_train||0} / ${m.n_test||0}</div></div>
        <div class="ml-meta-item"><div class="lbl">Train AUC</div><div class="val">${trAuc!=null?trAuc.toFixed(3):'—'}</div></div>
        <div class="ml-meta-item"><div class="lbl">Test AUC</div><div class="val" style="color:${aucColor}">${teAuc!=null?teAuc.toFixed(3):'—'}</div></div>
        <div class="ml-meta-item"><div class="lbl">Base WR</div><div class="val">${m.test?.base_wr!=null?(m.test.base_wr*100).toFixed(1)+'%':'—'}</div></div>`;
      gridEl.style.display='';
      const thresholds = m.test?.thresholds||[];
      if (thresholds.length) {
        thrTable.innerHTML = `<thead><tr><th>P(win) ≥</th><th>Trades Kept</th><th>Keep %</th><th>Win Rate</th></tr></thead><tbody>
          ${thresholds.map(r=>`<tr><td>${r.p.toFixed(2)}</td><td>${r.n}</td><td>${(r.keep_frac*100).toFixed(1)}%</td><td style="color:${r.wr!=null&&r.wr>0.52?'var(--green-light)':r.wr!=null&&r.wr<0.5?'var(--red-light)':'var(--text2)'}">${r.wr!=null?(r.wr*100).toFixed(1)+'%':'—'}</td></tr>`).join('')}
          </tbody>`;
        thrWrap.style.display='';
      }
    }
  } catch(e) {
    statusEl.innerHTML = `<div class="ml-dot untrained"></div><div style="font-size:.8rem;color:var(--red-light)">Error: ${e.message}</div>`;
  }
}

async function loadMlFiles() {
  const listEl = document.getElementById('mlFileList'), countEl = document.getElementById('mlFileCount');
  listEl.innerHTML = '<div style="padding:12px;color:var(--text3);font-size:.77rem">Loading…</div>';
  try {
    mlFiles = await apiFetch('?api=ml_files');
    if (!mlFiles.length) { listEl.innerHTML='<div style="padding:12px;color:var(--text3)">No trade logs found in data/</div>'; countEl.textContent=''; return; }
    countEl.textContent = `(${mlFiles.length})`;
    listEl.innerHTML = mlFiles.map(f=>`<label class="file-item"><input type="checkbox" class="ml-file-chk" value="${f.file}" checked><span class="fname">${f.file}</span><span class="fmeta">${f.labeled} trades · ${fmtTs(f.mtime)}</span></label>`).join('');
  } catch(e) { listEl.innerHTML=`<div style="padding:12px;color:var(--red-light)">Error: ${e.message}</div>`; }
}

function mlSelectAll(checked) { document.querySelectorAll('.ml-file-chk').forEach(c=>c.checked=checked); }

async function trainModel() {
  const btn = document.getElementById('mlTrainBtn'), out = document.getElementById('mlOutput');
  const include = Array.from(document.querySelectorAll('.ml-file-chk:checked')).map(c=>c.value);
  if (!include.length) { alert('Select at least one training file.'); return; }
  btn.disabled=true; btn.innerHTML='<div class="spinner"></div> Training…';
  out.className='ml-output'; out.textContent='Running training…\n';
  try {
    const res = await apiPost('?api=ml_train', {
      model: document.getElementById('fMlModel').value,
      threshold: parseFloat(document.getElementById('fMlTrainThreshold').value)||0.50,
      test_frac: parseFloat(document.getElementById('fMlTestFrac').value)||0.20,
      min_trades: parseInt(document.getElementById('fMlMinTrades').value)||200,
      history_weight: parseFloat(document.getElementById('fMlHistWeight').value)||0.5,
      no_history: document.getElementById('tNoHistory').checked,
      include,
    });
    out.textContent = `[${res.success?'SUCCESS':'FAILED'}] exit=${res.return_code} · elapsed=${res.elapsed_sec}s\n$ ${res.command}\n${'─'.repeat(50)}\n${res.output||'(no output)'}`;
    if (res.success) await refreshMlStatus();
  } catch(e) { out.textContent='Error: '+e.message; }
  btn.disabled=false; btn.innerHTML='🔬 Train Model';
}

async function fetchHistory() {
  const btn=document.getElementById('fetchHistBtn'), out=document.getElementById('fetchHistOutput');
  btn.disabled=true; btn.innerHTML='<div class="spinner"></div> Fetching…';
  out.style.display=''; out.className='ml-output'; out.textContent='Fetching…\n';
  try {
    const res = await apiPost('?api=fetch_history', { hours: parseFloat(document.getElementById('fHistHours').value)||48, app_id: parseInt(document.getElementById('fHistAppId').value)||1089 });
    out.textContent = `[${res.success?'SUCCESS':'FAILED'}] elapsed=${res.elapsed_sec}s\n${res.output||'(no output)'}`;
    if (res.success) await loadMlFiles();
  } catch(e) { out.textContent='Error: '+e.message; }
  btn.disabled=false; btn.innerHTML='📡 Fetch & Simulate';
}

// ─── MARKET SCANNER ──────────────────────────────────────────────────────────
let scanSource = null, scanResults = [];

function startScan() {
  const hours = document.getElementById('fScanHours').value;
  const appId = document.getElementById('fScanAppId').value;
  if (scanSource) { scanSource.close(); scanSource=null; }
  scanResults=[];
  document.getElementById('scanBtn').disabled=true;
  document.getElementById('scanBtn').innerHTML='<div class="spinner" style="width:14px;height:14px;border-width:2px"></div> Scanning...';
  document.getElementById('scanStopBtn').style.display='';
  document.getElementById('scanProgress').style.display='flex';
  document.getElementById('scanProgressFill').style.width='0%';
  document.getElementById('scanProgressText').textContent='Connecting...';
  document.getElementById('scanResultsWrap').style.display='';
  document.getElementById('scanResults').innerHTML='';

  scanSource = new EventSource(`?api=scan_market&hours=${hours}&app_id=${appId}`);
  scanSource.onmessage = ev => {
    let d; try{d=JSON.parse(ev.data);}catch(e){return;}
    if (d.type==='start') document.getElementById('scanProgressText').textContent=`Scanning ${d.symbols?.length||0} symbols...`;
    else if (d.type==='progress') { document.getElementById('scanProgressFill').style.width=Math.round((d.step/d.total)*100)+'%'; document.getElementById('scanProgressText').textContent=d.message; }
    else if (d.type==='result') { scanResults.push(d); renderScanCard(d, document.getElementById('scanResults')); document.getElementById('scanResultCount').textContent=`(${scanResults.length})`; }
    else if (d.type==='done') { finishScan(); sortScanResults(); if(document.getElementById('tAutoExclude').checked) applyAllScanResults(); }
  };
  scanSource.onerror = () => { finishScan(); if(scanResults.length) sortScanResults(); };
}

function stopScan() { if(scanSource){scanSource.close();scanSource=null;} finishScan(); }
function finishScan() {
  document.getElementById('scanBtn').disabled=false;
  document.getElementById('scanBtn').innerHTML='🔍 Manual Scan';
  document.getElementById('scanStopBtn').style.display='none';
  document.getElementById('scanProgress').style.display='none';
  document.getElementById('scanTimestamp').textContent='Scanned at '+new Date().toLocaleTimeString();
  if(scanSource){scanSource.close();scanSource=null;}
}
function sortScanResults() {
  scanResults.sort((a,b)=>(b.recommendation?.tradability||0)-(a.recommendation?.tradability||0));
  const grid=document.getElementById('scanResults'); grid.innerHTML='';
  scanResults.forEach(d=>renderScanCard(d,grid));
}

function renderScanCard(d, container) {
  if (d.status==='NO_DATA') return;
  const r=d.recommendation||{}, v=d.volatility||{}, dg=d.digits||{}, p=d.patterns||{};
  const sig=r.entry_signal||'WAIT', trad=r.tradability||0;
  let cardClass='scan-card', badgeClass='scan-badge wait-badge', badgeText=sig.replace(/_/g,' ');
  if (sig==='STRONG_ENTRY'){cardClass+=' strong';badgeClass='scan-badge strong-entry';}
  else if (sig==='GOOD_ENTRY'){cardClass+=' good';badgeClass='scan-badge good-entry';}
  else if (sig==='DO_NOT_ENTER'){cardClass+=' danger';badgeClass='scan-badge no-entry';}
  const tradColor=trad>=70?'var(--green-light)':trad>=45?'var(--amber)':'var(--red-light)';
  const regColors={MEAN_REVERTING:'var(--green-light)',TRENDING:'var(--teal)',CHOPPY:'var(--red-light)',UNKNOWN:'var(--text3)'};
  const volColors={LOW:'var(--green-light)',MODERATE:'var(--teal)',HIGH:'var(--amber)',EXTREME:'var(--red-light)',UNKNOWN:'var(--text3)'};
  const algoNames={alphabloom:'AlphaBloom',pulse:'Pulse',ensemble:'Ensemble',novaburst:'NovaBurst',adaptive:'Adaptive'};
  const tsNames={even_odd:'Even/Odd',rise_fall_roll:'Rise/Fall Roll',rise_fall_zigzag:'Rise/Fall Zigzag',higher_lower_roll:'H/L Roll',higher_lower_zigzag:'H/L Zigzag',over_under_roll:'O/U Roll',touch_notouch_zigzag:'Touch/NoTouch Zigzag'};
  const card=document.createElement('div'); card.className=cardClass;
  card.innerHTML=`<div class="scan-head"><span class="scan-sym">${d.symbol}</span><span class="${badgeClass}">${badgeText}</span></div>
    <div class="scan-metrics">
      <div class="scan-metric"><div class="sm-label">Regime</div><div class="sm-value" style="color:${regColors[d.regime]||'var(--text)'}">${d.regime||'—'}</div></div>
      <div class="scan-metric"><div class="sm-label">Volatility</div><div class="sm-value" style="color:${volColors[v.level]||'var(--text)'}">${v.level||'—'}</div></div>
      <div class="scan-metric"><div class="sm-label">ATR %ile</div><div class="sm-value">${v.atr_percentile!=null?v.atr_percentile.toFixed(0)+'%':'—'}</div></div>
      <div class="scan-metric"><div class="sm-label">P(even)</div><div class="sm-value">${dg.p_even!=null?(dg.p_even*100).toFixed(1)+'%':'—'}</div></div>
      <div class="scan-metric"><div class="sm-label">Bias</div><div class="sm-value" style="color:${dg.is_biased?'var(--green-light)':'var(--text3)'}">${dg.is_biased?'YES':'no'}</div></div>
      <div class="scan-metric"><div class="sm-label">Momentum</div><div class="sm-value">${v.momentum!=null?(v.momentum>=0?'+':'')+v.momentum.toFixed(4):'—'}</div></div>
    </div>
    <div class="scan-patterns">
      <span class="scan-pat ${(p.pulse?.score||0)>=0.4?'active':''}">Pulse ${p.pulse?.score!=null?p.pulse.score.toFixed(2):'—'}</span>
      <span class="scan-pat ${(p.rollcake?.score||0)>=0.3?'active':''}">Roll ${p.rollcake?.score!=null?p.rollcake.score.toFixed(2):'—'}</span>
      <span class="scan-pat ${(p.zigzag?.score||0)>=0.3?'active':''}">Zigzag ${p.zigzag?.score!=null?p.zigzag.score.toFixed(2):'—'}</span>
    </div>
    <div class="scan-rec">
      <div class="scan-rec-text"><strong>${algoNames[r.algorithm]||r.algorithm||'—'}</strong> + ${tsNames[r.trade_strategy]||r.trade_strategy||'—'}</div>
      ${sig!=='DO_NOT_ENTER'?`<button class="scan-use-btn" onclick="applyScanRec('${r.algorithm}','${r.trade_strategy}')">Use ↗</button>`:''}
    </div>
    <div class="trad-bar"><div class="trad-fill" style="width:${trad}%;background:${tradColor}"></div></div>
    <div style="text-align:right;font-size:.67rem;color:var(--text4);margin-top:4px;font-family:var(--mono)">Tradability: ${trad}/100</div>`;
  container.appendChild(card);
}

function applyScanRec(algo, ts) {
  const algoEl=document.getElementById('fStrategy');
  if(algoEl){algoEl.value=algo;onStrategyChange();}
  const tsEl=document.getElementById('fTradeStrategy');
  if(tsEl) tsEl.value=ts;
  updateCmd(); switchTab('control', document.querySelector('[data-tab="control"]'));
}

function applyAllScanResults() {
  if (!scanResults.length) return;
  const autoExclude=document.getElementById('tAutoExclude').checked;
  const autoStrategy=document.getElementById('tAutoStrategy').checked;
  scanResults.forEach(d => {
    const sig=d.recommendation?.entry_signal||'WAIT';
    const chk=document.getElementById('sym_'+d.symbol);
    if (chk) chk.checked = !(autoExclude && sig==='DO_NOT_ENTER');
  });
  updateSymCount();
  if (autoStrategy && scanResults.length) {
    const best=scanResults.find(d=>(d.recommendation?.entry_signal||'')!=='DO_NOT_ENTER');
    if (best?.recommendation) {
      const algoEl=document.getElementById('fStrategy');
      if(algoEl){algoEl.value=best.recommendation.algorithm;onStrategyChange();}
      const tsEl=document.getElementById('fTradeStrategy');
      if(tsEl) tsEl.value=best.recommendation.trade_strategy;
    }
  }
  updateCmd();
}

// ─── DAEMON ───────────────────────────────────────────────────────────────────
async function startDaemon() {
  const btn=document.getElementById('daemonStartBtn');
  btn.disabled=true; btn.innerHTML='<div class="spinner" style="width:14px;height:14px;border-width:2px"></div> Starting...';
  try {
    await fetch('?api=daemon_start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval:parseInt(document.getElementById('fDaemonInterval').value),app_id:parseInt(document.getElementById('fDaemonAppId').value),hours:parseFloat(document.getElementById('fDaemonHours').value)})});
  } catch(e) {}
  setTimeout(refreshDaemonStatus, 1500);
}
async function stopDaemon() {
  try { await fetch('?api=daemon_stop',{method:'POST'}); } catch(e) {}
  setTimeout(refreshDaemonStatus, 1000);
}
async function refreshDaemonStatus() {
  try {
    const data=await apiFetch('?api=daemon_status');
    const dot=document.getElementById('daemonDot'), lbl=document.getElementById('daemonLabel');
    const startBtn=document.getElementById('daemonStartBtn'), stopBtn=document.getElementById('daemonStopBtn');
    const sidebarDot=document.getElementById('sidebarDaemonDot'), topDot=document.getElementById('topDaemonDot');
    const sfStatus=document.getElementById('sfDaemonStatus');
    if (data.running) {
      dot.className='status-dot on'; lbl.textContent='Running'; lbl.style.color='var(--green-light)';
      startBtn.style.display='none'; stopBtn.style.display='';
      sidebarDot.className='si-dot on'; topDot.className='dot on';
      if(sfStatus){sfStatus.textContent=`running (${data.file_age_seconds}s)`;sfStatus.style.color='#68d391';}
    } else {
      dot.className='status-dot off'; lbl.textContent='Stopped'; lbl.style.color='var(--text3)';
      startBtn.style.display=''; startBtn.disabled=false; startBtn.innerHTML='▶ Start Daemon';
      stopBtn.style.display='none';
      sidebarDot.className='si-dot off'; topDot.className='dot off';
      if(sfStatus){sfStatus.textContent='stopped';sfStatus.style.color='#fc8181';}
    }
    if (data.file_exists && data.file_age_seconds!==null && data.running) lbl.textContent+=` (data ${data.file_age_seconds}s ago)`;
  } catch(e) {}
}

async function refreshFromDaemon() {
  try {
    const wrap=document.getElementById('scanResultsWrap');
    const grid=document.getElementById('scanResults');
    wrap.style.display='';
    grid.innerHTML='<div style="padding:20px;color:var(--text3);display:flex;align-items:center"><div class="spinner" style="margin-right:8px;border-width:2px;width:14px;height:14px"></div>Loading real-time scan results from daemon...</div>';
    
    const data=await apiFetch('?api=daemon_scan');
    if (data.error) { alert(data.error); return; }
    const results=data.results||[];
    scanResults=[];
    grid.innerHTML=''; wrap.style.display='';
    results.forEach(d => {
      d.recommendation=recommendForSymbol(d); d.type='result'; d.status=d.status||'OK';
      scanResults.push(d); renderScanCard(d, grid);
    });
    document.getElementById('scanResultCount').textContent=`(${scanResults.length})`;
    document.getElementById('scanTimestamp').textContent=`Daemon: ${data.updated_at||new Date().toLocaleTimeString()}`;
    if (document.getElementById('tAutoExclude').checked||autoScanRunning) applyAllScanResults();
  } catch(e) { console.error('Daemon refresh failed:', e); }
}

function recommendForSymbol(d) {
  const allowedAlgos=[...document.querySelectorAll('.algoFilterChk:checked')].map(c=>c.value);
  const allowedTS=[...document.querySelectorAll('.tsFilterChk:checked')].map(c=>c.value);
  const v=d.volatility||{}, dg=d.digits||{}, p=d.patterns||{};
  const volLevel=v.level||'UNKNOWN', trad=d.tradability||0, biasMag=dg.bias_magnitude||0;
  const pickAlgo=pref=>allowedAlgos.includes(pref)?pref:(allowedAlgos[0]||'adaptive');
  const pickTS=pref=>allowedTS.includes(pref)?pref:(allowedTS[0]||'even_odd');
  if (volLevel==='EXTREME') return {algorithm:pickAlgo('adaptive'),trade_strategy:pickTS('even_odd'),entry_signal:'DO_NOT_ENTER',tradability:trad};
  if (volLevel==='HIGH') {
    if (dg.is_biased && biasMag>=0.10 && (p.pulse?.score||0)>=0.65) return matchFilter([{a:'pulse',t:'even_odd',s:'GOOD_ENTRY'}],allowedAlgos,allowedTS,trad);
    return {algorithm:pickAlgo('adaptive'),trade_strategy:pickTS('even_odd'),entry_signal:'DO_NOT_ENTER',tradability:trad};
  }
  const candidates=[], ps=p.pulse?.score||0, rs=p.rollcake?.score||0, zs=p.zigzag?.score||0, regime=d.regime||'UNKNOWN';
  if (regime==='MEAN_REVERTING' && (volLevel==='LOW'||volLevel==='MODERATE')) {
    if (dg.is_biased && biasMag>=0.06 && ps>=0.6) candidates.push({a:'pulse',t:'even_odd',s:'STRONG_ENTRY'});
    if (dg.is_biased && biasMag>=0.06 && ps>=0.4) candidates.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    if (dg.is_biased && biasMag>=0.06) candidates.push({a:'alphabloom',t:'even_odd',s:'GOOD_ENTRY'});
    if (rs>=0.70 && volLevel==='LOW') candidates.push({a:'pulse',t:'rise_fall_roll',s:'GOOD_ENTRY'});
    candidates.push({a:'ensemble',t:'even_odd',s:'WAIT'});
  }
  if (regime==='TRENDING') {
    if (dg.is_biased && ps>=0.55) candidates.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    if (dg.is_biased && biasMag>=0.08) candidates.push({a:'alphabloom',t:'even_odd',s:'GOOD_ENTRY'});
    if (volLevel==='LOW' && rs>=0.70) candidates.push({a:'pulse',t:'rise_fall_roll',s:'GOOD_ENTRY'});
    candidates.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  }
  if (regime==='CHOPPY') {
    if (dg.is_biased && biasMag>=0.08 && ps>=0.55) candidates.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    candidates.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  }
  if (dg.is_biased && ps>=0.55) candidates.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
  candidates.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  return matchFilter(candidates,allowedAlgos,allowedTS,trad);
}

function matchFilter(candidates,allowedAlgos,allowedTS,trad) {
  for (const c of candidates) { if(allowedAlgos.includes(c.a)&&allowedTS.includes(c.t)) return{algorithm:c.a,trade_strategy:c.t,entry_signal:c.s,tradability:trad}; }
  return{algorithm:allowedAlgos[0]||'adaptive',trade_strategy:allowedTS[0]||'even_odd',entry_signal:'WAIT',tradability:trad};
}

// ─── AUTO-SCAN ───────────────────────────────────────────────────────────────
let autoScanTimer=null, autoScanRunning=false;
function toggleAutoScan() {
  if (autoScanRunning) {
    autoScanRunning=false; clearInterval(autoScanTimer); autoScanTimer=null;
    document.getElementById('autoScanBtn').innerHTML='🔁 Start Auto-Refresh';
    document.getElementById('autoScanBtn').className='btn btn-ghost';
  } else {
    const intervalMin=parseFloat(document.getElementById('fScanInterval').value)||0;
    if (intervalMin<=0) { alert('Set Auto-Refresh Interval first.'); return; }
    autoScanRunning=true;
    document.getElementById('autoScanBtn').innerHTML='⏹ Stop Auto-Refresh';
    document.getElementById('autoScanBtn').className='btn btn-danger';
    refreshFromDaemon();
    autoScanTimer=setInterval(refreshFromDaemon, intervalMin*60*1000);
  }
}

// ─── UTILS ───────────────────────────────────────────────────────────────────
function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'})
       + ' ' + d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
}
function computeDuration(start, end) {
  if (!start) return '—';
  const diff = Math.floor(((end?end:Date.now()/1000)-start));
  if (diff<60) return diff+'s';
  if (diff<3600) return Math.floor(diff/60)+'m '+String(diff%60).padStart(2,'0')+'s';
  return Math.floor(diff/3600)+'h '+Math.floor((diff%3600)/60)+'m';
}

// ─── BOOT ────────────────────────────────────────────────────────────────────
initSymChecklist();
onStrategyChange();
init();
refreshDaemonStatus();
</script>
</body>
</html>