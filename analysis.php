<?php
/**
 * Deriv Bot Session Dashboard + Bot Control
 * Redesigned — SSE logs, fixed P&L sync, improved charts, new theme
 */

$DATA_DIR  = __DIR__ . '/data';
$BOT_DIR   = __DIR__;
$TMUX_NAME = 'bbot';

if (isset($_GET['api'])) {
    header('Content-Type: application/json');
    header('Cache-Control: no-cache');

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
            // Compute net_pnl from trades if summary is stale
            $net_pnl = $sum['net_pnl'] ?? 0;
            $wins = $sum['wins'] ?? 0;
            $losses = $sum['losses'] ?? 0;
            $trade_count = $sum['trade_count'] ?? 0;
            if (isset($d['trades']) && is_array($d['trades']) && count($d['trades']) > 0) {
                $net_pnl = array_sum(array_column($d['trades'], 'profit'));
                $wins = count(array_filter($d['trades'], fn($t) => ($t['result']??'') === 'win'));
                $losses = count(array_filter($d['trades'], fn($t) => ($t['result']??'') === 'loss'));
                $trade_count = count($d['trades']);
            }
            $win_rate = $trade_count > 0 ? $wins / $trade_count : 0;
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
                'trade_count'    => $trade_count,
                'wins'           => $wins,
                'losses'         => $losses,
                'net_pnl'        => round($net_pnl, 2),
                'win_rate'       => round($win_rate, 4),
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
            exec("tmux capture-pane -t " . escapeshellarg($TMUX_NAME) . " -p -S -50 2>&1", $logOut);
            $logs = implode("\n", $logOut);
        }
        echo json_encode(['running'=>$running,'tmux'=>$TMUX_NAME,'logs'=>$logs]);
        exit;
    }

    // SSE endpoint for live bot logs
    if ($_GET['api'] === 'bot_logs_sse') {
        header('Content-Type: text/event-stream');
        header('Cache-Control: no-cache');
        header('X-Accel-Buffering: no');
        header('Connection: keep-alive');
        if (ob_get_level()) ob_end_clean();

        $lastLog = '';
        $timeout = time() + 300; // 5 min max
        while (time() < $timeout) {
            $chk = []; $chkCode = -1;
            exec("tmux has-session -t " . escapeshellarg($TMUX_NAME) . " 2>&1", $chk, $chkCode);
            $running = $chkCode === 0;
            $logs = '';
            if ($running) {
                $logOut = [];
                exec("tmux capture-pane -t " . escapeshellarg($TMUX_NAME) . " -p -S -80 2>&1", $logOut);
                $logs = implode("\n", $logOut);
            }
            if ($logs !== $lastLog) {
                $lastLog = $logs;
                echo "data: " . json_encode(['running' => $running, 'logs' => $logs]) . "\n\n";
                flush();
            } else {
                // Heartbeat
                echo ": heartbeat\n\n";
                flush();
            }
            if (connection_aborted()) break;
            sleep(2);
        }
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
        $script .= "echo \"Starting: $cmd\"\necho \"---\"\n";
        $script .= "$cmd\n";
        $script .= "EXIT=\$?\necho \"\"\necho \"=== Bot exited (code \$EXIT) — press Enter ===\"\nread\n";
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
        if (file_exists($meta)) { $m = json_decode(@file_get_contents($meta),true); if (is_array($m)) $resp['meta']=$m; }
        echo json_encode($resp);
        exit;
    }

    if ($_GET['api'] === 'ml_train' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $model    = in_array($body['model']??'',['logreg','gbm'])?$body['model']:'logreg';
        $thr      = floatval($body['threshold']??0.50);
        $testFrac = floatval($body['test_frac']??0.20);
        $minTr    = intval($body['min_trades']??200);
        $histWt   = floatval($body['history_weight']??0.5);
        $noHist   = !empty($body['no_history']);
        $incRaw   = $body['include']??[];
        $include  = [];
        if (is_array($incRaw)) {
            foreach ($incRaw as $name) {
                $b = basename((string)$name);
                if (preg_match('/^[A-Za-z0-9._-]+\.json$/',$b) && $b!=='ml_filter.json') $include[]=$b;
            }
        }
        $cmd = sprintf("cd %s && python3 train_filter.py --model %s --threshold %.3f --test-frac %.3f --min-trades %d --history-weight %.2f",
            escapeshellarg($BOT_DIR),escapeshellarg($model),$thr,$testFrac,$minTr,$histWt);
        if ($noHist) $cmd .= ' --no-history';
        if ($include) $cmd .= ' --include '.escapeshellarg(implode(',',$include));
        $cmd .= ' 2>&1';
        $t0=microtime(true); $output=[]; $ret=-1;
        exec($cmd,$output,$ret);
        $elapsed=microtime(true)-$t0;
        $meta=null; $metaPath=$DATA_DIR.'/ml_filter.json';
        if ($ret===0 && file_exists($metaPath)) $meta=json_decode(@file_get_contents($metaPath),true);
        echo json_encode(['success'=>$ret===0,'return_code'=>$ret,'elapsed_sec'=>round($elapsed,2),'command'=>$cmd,'output'=>implode("\n",$output),'meta'=>$meta]);
        exit;
    }

    if ($_GET['api'] === 'fetch_history' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $hours = floatval($body['hours']??48);
        $appId = intval($body['app_id']??1089);
        $cmd = sprintf("cd %s && python3 fetch_history.py --app-id %d --hours %.0f 2>&1",escapeshellarg($BOT_DIR),$appId,$hours);
        $t0=microtime(true); $output=[]; $ret=-1;
        exec($cmd,$output,$ret);
        $elapsed=microtime(true)-$t0;
        echo json_encode(['success'=>$ret===0,'return_code'=>$ret,'elapsed_sec'=>round($elapsed,2),'command'=>$cmd,'output'=>implode("\n",$output)]);
        exit;
    }

    if ($_GET['api'] === 'scan_market') {
        header('Content-Type: text/event-stream');
        header('Cache-Control: no-cache');
        header('X-Accel-Buffering: no');
        if (ob_get_level()) ob_end_clean();
        $hours = floatval($_GET['hours']??1);
        $appId = intval($_GET['app_id']??1089);
        if ($hours<0.25||$hours>24) $hours=1;
        $cmd = sprintf("cd %s && python3 market_scanner.py --app-id %d --hours %.2f 2>/dev/null",escapeshellarg($BOT_DIR),$appId,$hours);
        $proc = popen($cmd,'r');
        if (!$proc) { echo "data: ".json_encode(['type'=>'error','message'=>'Failed to start scanner'])."\n\n"; flush(); exit; }
        while (!feof($proc)) {
            $line = fgets($proc);
            if ($line===false) break;
            $line = trim($line);
            if ($line==='') continue;
            echo "data: $line\n\n"; flush();
        }
        pclose($proc); exit;
    }

    if ($_GET['api'] === 'daemon_start' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = json_decode(file_get_contents('php://input'), true) ?? [];
        $interval = intval($body['interval']??30);
        $appId    = intval($body['app_id']??1089);
        if ($interval<5) $interval=5;
        if ($interval>300) $interval=300;
        $DAEMON_TMUX = 'market_daemon';
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t ".escapeshellarg($DAEMON_TMUX)." 2>&1",$chk,$chkCode);
        if ($chkCode===0) { exec("tmux send-keys -t ".escapeshellarg($DAEMON_TMUX)." C-c 2>&1"); usleep(500000); exec("tmux kill-session -t ".escapeshellarg($DAEMON_TMUX)." 2>&1"); sleep(1); }
        $cmd = sprintf("cd %s && python3 market_daemon.py --app-id %d --interval %d",escapeshellarg($BOT_DIR),$appId,$interval);
        exec("tmux new-session -d -s ".escapeshellarg($DAEMON_TMUX)." ".escapeshellarg($cmd)." 2>&1",$out,$ret);
        sleep(2); $v=[]; $vc=-1;
        exec("tmux has-session -t ".escapeshellarg($DAEMON_TMUX)." 2>&1",$v,$vc);
        echo json_encode(['success'=>($vc===0),'interval'=>$interval]);
        exit;
    }

    if ($_GET['api'] === 'daemon_stop' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $DAEMON_TMUX = 'market_daemon';
        exec("tmux send-keys -t ".escapeshellarg($DAEMON_TMUX)." C-c 2>&1");
        usleep(500000);
        exec("tmux kill-session -t ".escapeshellarg($DAEMON_TMUX)." 2>&1");
        echo json_encode(['success'=>true]);
        exit;
    }

    if ($_GET['api'] === 'daemon_status') {
        $DAEMON_TMUX = 'market_daemon';
        $chk=[]; $chkCode=-1;
        exec("tmux has-session -t ".escapeshellarg($DAEMON_TMUX)." 2>&1",$chk,$chkCode);
        $scanFile = $BOT_DIR.'/data/market_scan.json';
        $fileAge = file_exists($scanFile) ? time()-filemtime($scanFile) : null;
        echo json_encode(['running'=>($chkCode===0),'file_exists'=>file_exists($scanFile),'file_age_seconds'=>$fileAge]);
        exit;
    }

    if ($_GET['api'] === 'daemon_scan') {
        $scanFile = $BOT_DIR.'/data/market_scan.json';
        if (!file_exists($scanFile)) { echo json_encode(['error'=>'No scan data. Start the Market Daemon first.']); exit; }
        header('Content-Type: application/json');
        echo file_get_contents($scanFile);
        exit;
    }

    // Background process status check (tmux sessions)
    if ($_GET['api'] === 'process_status') {
        $sessions_check = [];
        foreach (['bbot','market_daemon'] as $name) {
            $chk=[]; $code=-1;
            exec("tmux has-session -t ".escapeshellarg($name)." 2>&1",$chk,$code);
            $sessions_check[$name] = $code === 0;
        }
        echo json_encode(['sessions' => $sessions_check, 'time' => time()]);
        exit;
    }

    http_response_code(400); echo json_encode(['error'=>'Unknown endpoint']); exit;
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Deriv Bot — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;500;600;700;800&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;1,9..40,400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  /* Core palette — rich dark indigo */
  --ink:#080b14;
  --ink1:#0d1120;
  --ink2:#121827;
  --ink3:#1a2236;
  --ink4:#212d44;
  --ink5:#2d3f5c;

  --rim:#2a3650;
  --rim2:#3d5070;

  --text:#dce6f5;
  --text2:#8a9bbf;
  --text3:#4d607e;

  /* Accent system */
  --lime:#a8f03d;
  --lime2:#a8f03d28;
  --lime3:#7ab82e;
  --crimson:#f0503d;
  --crimson2:#f0503d22;
  --sky:#3db8f0;
  --sky2:#3db8f022;
  --gold:#f0c53d;
  --gold2:#f0c53d22;
  --violet:#9b7cff;
  --violet2:#9b7cff22;
  --teal:#3df0d4;
  --teal2:#3df0d422;

  --font:'Syne',sans-serif;
  --body:'DM Sans',sans-serif;
  --mono:'Space Mono',monospace;

  --r:8px;
  --r2:12px;
  --r3:16px;
}

html{font-size:13px;scroll-behavior:smooth}
body{background:var(--ink);color:var(--text);font-family:var(--body);min-height:100vh;overflow-x:hidden;line-height:1.5}

/* Subtle grid bg */
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.04;
  background-image:linear-gradient(var(--rim) 1px,transparent 1px),linear-gradient(90deg,var(--rim) 1px,transparent 1px);
  background-size:40px 40px;
}

/* Subtle top glow */
body::after{
  content:'';position:fixed;top:0;left:50%;transform:translateX(-50%);
  width:800px;height:400px;border-radius:50%;
  background:radial-gradient(ellipse,#3d6af015 0%,transparent 70%);
  pointer-events:none;z-index:0;
}

.wrap{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:20px 18px}

/* ── TOPBAR ── */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:0 0 16px;margin-bottom:4px;border-bottom:1px solid var(--rim)}
.logo{display:flex;align-items:center;gap:12px}
.logo-mark{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,var(--lime),var(--teal));display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:.85rem;font-weight:700;color:var(--ink);letter-spacing:-.05em}
.logo-text{font-family:var(--font);font-size:1.1rem;font-weight:800;letter-spacing:-.02em;color:var(--text)}
.logo-sub{font-size:.7rem;color:var(--text3);margin-top:1px;font-family:var(--body);font-weight:400}
.topbar-right{display:flex;align-items:center;gap:10px}

/* Process pills */
.proc-pills{display:flex;gap:6px;align-items:center}
.proc-pill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;background:var(--ink2);border:1px solid var(--rim);font-size:.7rem;font-family:var(--mono);color:var(--text3);transition:all .2s}
.proc-pill.active{border-color:var(--lime);color:var(--lime);background:var(--lime2)}
.proc-pill .pip{width:6px;height:6px;border-radius:50%;background:currentColor}
.proc-pill.active .pip{animation:blink 1.8s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

.btn{font-family:var(--font);font-size:.8rem;font-weight:700;padding:7px 16px;border-radius:var(--r);border:none;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;letter-spacing:-.01em}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important;box-shadow:none!important}
.btn-lime{background:var(--lime);color:var(--ink)}
.btn-lime:hover:not(:disabled){background:#c4ff5a;transform:translateY(-1px);box-shadow:0 4px 20px #a8f03d40}
.btn-red{background:var(--crimson);color:#fff}
.btn-red:hover:not(:disabled){background:#ff6a58;transform:translateY(-1px)}
.btn-ghost{background:var(--ink2);color:var(--text2);border:1px solid var(--rim)}
.btn-ghost:hover:not(:disabled){background:var(--ink3);border-color:var(--rim2);color:var(--text)}
.btn-sky{background:var(--sky2);color:var(--sky);border:1px solid var(--sky)44}
.btn-sky:hover:not(:disabled){background:#3db8f030;transform:translateY(-1px)}

/* ── TABS ── */
.tabs{display:flex;gap:2px;margin:14px 0 20px;background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:4px;width:fit-content}
.tab{font-family:var(--font);font-size:.82rem;font-weight:700;padding:8px 18px;border:none;background:none;color:var(--text3);cursor:pointer;border-radius:6px;transition:all .15s;display:flex;align-items:center;gap:7px;letter-spacing:-.01em}
.tab:hover{color:var(--text2);background:var(--ink3)}
.tab.active{background:var(--ink3);color:var(--text);box-shadow:inset 0 0 0 1px var(--rim2)}
.tab-pip{width:7px;height:7px;border-radius:50%;background:var(--text3);transition:all .2s}
.tab.active .tab-pip{background:var(--lime);box-shadow:0 0 8px var(--lime)}
.tab-content{display:none}
.tab-content.active{display:block}

/* ── SESSION STRIP ── */
.section-label{font-family:var(--font);font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.section-label::after{content:'';flex:1;height:1px;background:var(--rim)}

.session-strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:8px;margin-bottom:22px}
.session-strip::-webkit-scrollbar{height:4px}
.session-strip::-webkit-scrollbar-track{background:transparent}
.session-strip::-webkit-scrollbar-thumb{background:var(--rim);border-radius:2px}

.scard{flex:0 0 260px;background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:14px 16px;cursor:pointer;transition:all .18s;position:relative;overflow:hidden}
.scard::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:transparent;transition:background .2s}
.scard:hover{border-color:var(--rim2);transform:translateY(-2px);box-shadow:0 8px 24px #00000040}
.scard.sel{border-color:var(--lime);box-shadow:0 0 0 1px var(--lime)40,0 8px 24px #00000040}
.scard.sel::before{background:var(--lime)}
.scard.live::after{content:'LIVE';position:absolute;top:10px;right:10px;font-family:var(--mono);font-size:.58rem;font-weight:700;padding:2px 6px;background:var(--lime2);color:var(--lime);border-radius:3px;animation:blink 2s infinite}

.sc-id{font-family:var(--mono);font-size:.82rem;font-weight:700;color:var(--text);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.sc-meta{font-size:.7rem;color:var(--text3);margin-bottom:10px;display:flex;align-items:center;gap:6px}
.mode-badge{font-size:.6rem;font-weight:700;padding:1px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.mode-badge.demo{background:var(--sky2);color:var(--sky)}
.mode-badge.live{background:var(--lime2);color:var(--lime)}
.sc-row{display:flex;gap:14px}
.sc-stat{display:flex;flex-direction:column;gap:1px}
.sc-stat .k{font-size:.6rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text3)}
.sc-stat .v{font-family:var(--mono);font-size:.82rem;font-weight:700}
.g{color:var(--lime)}.r{color:var(--crimson)}.a{color:var(--gold)}.s{color:var(--sky)}

/* ── EMPTY STATE ── */
.empty-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:80px 20px;text-align:center;gap:14px}
.empty-msg .em-icon{font-size:2.5rem;opacity:.3}
.empty-msg p{color:var(--text3);font-size:.9rem;max-width:360px;line-height:1.7}

/* ── STAT CARDS ── */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.kpi{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:14px 16px;position:relative;overflow:hidden;transition:border-color .2s}
.kpi:hover{border-color:var(--rim2)}
.kpi .kpi-label{font-size:.63rem;text-transform:uppercase;letter-spacing:.09em;color:var(--text3);font-weight:600;margin-bottom:6px}
.kpi .kpi-val{font-family:var(--mono);font-size:1.3rem;font-weight:700;line-height:1;margin-bottom:4px}
.kpi .kpi-sub{font-size:.68rem;color:var(--text3)}
.kpi.hl::after{content:'';position:absolute;top:0;right:0;bottom:0;width:2px}
.kpi.hl-g{border-color:var(--lime)44}.kpi.hl-g::after{background:var(--lime)}
.kpi.hl-r{border-color:var(--crimson)44}.kpi.hl-r::after{background:var(--crimson)}

/* ── CONFIG BAR ── */
.cfgbar{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:12px 16px;margin-bottom:18px;display:flex;flex-wrap:wrap;gap:16px;align-items:center}
.cfg-item{display:flex;flex-direction:column;gap:2px}
.cfg-item .ck{font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:600}
.cfg-item .cv{font-family:var(--mono);font-size:.82rem;font-weight:600}
.sym-tags{display:flex;flex-wrap:wrap;gap:4px}
.sym-tag{font-family:var(--mono);font-size:.65rem;padding:2px 6px;border-radius:4px;background:var(--ink3);border:1px solid var(--rim2);color:var(--text2)}

/* ── CHART GRID ── */
.chart-grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:20px}
.chart-grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:20px}
@media(max-width:960px){.chart-grid{grid-template-columns:1fr}.chart-grid-3{grid-template-columns:1fr 1fr}}
@media(max-width:640px){.chart-grid-3{grid-template-columns:1fr}}
.chart-box{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:16px}
.chart-title{font-family:var(--font);font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:12px}
canvas{width:100%!important}

/* ── STREAK ── */
.streak-strip{display:flex;height:32px;border-radius:6px;overflow:hidden;background:var(--ink3);margin-bottom:12px}
.streak-seg{display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:.62rem;font-weight:700;min-width:12px;position:relative;color:rgba(255,255,255,.8);cursor:default;transition:opacity .1s}
.streak-seg:hover{opacity:.75}
.streak-seg.w{background:#7ab82e}.streak-seg.l{background:#c03020}
.streak-tip{position:absolute;bottom:calc(100%+6px);left:50%;transform:translateX(-50%);background:var(--ink4);border:1px solid var(--rim2);border-radius:6px;padding:5px 9px;font-size:.65rem;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity .1s;z-index:20}
.streak-seg:hover .streak-tip{opacity:1}

/* ── SYMBOL GRID ── */
.sym-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-bottom:20px}
.sym-card{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:12px 14px;transition:border-color .2s}
.sym-card:hover{border-color:var(--rim2)}
.sym-name{font-family:var(--mono);font-size:.88rem;font-weight:700;margin-bottom:8px}
.sym-row{display:flex;gap:12px;font-size:.72rem;margin-bottom:8px}
.sym-stat{display:flex;flex-direction:column;gap:1px}
.sym-stat .k{font-size:.58rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text3)}
.sym-stat .v{font-family:var(--mono);font-weight:700}
.sym-bar{height:4px;border-radius:2px;background:var(--ink3);overflow:hidden;margin-top:4px}
.sym-bar-fill{height:100%;border-radius:2px;transition:width .4s}

/* ── TRADE TABLE ── */
.trade-panel{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);overflow:hidden;margin-bottom:20px}
.trade-panel-hd{padding:12px 16px;border-bottom:1px solid var(--rim);display:flex;justify-content:space-between;align-items:center}
.trade-panel-hd h3{font-family:var(--font);font-size:.82rem;font-weight:700;color:var(--text)}
.trade-scroll{overflow-x:auto;max-height:420px;overflow-y:auto}
.trade-scroll::-webkit-scrollbar{width:4px;height:4px}
.trade-scroll::-webkit-scrollbar-thumb{background:var(--rim);border-radius:2px}
.ttbl{width:100%;border-collapse:collapse;min-width:740px}
.ttbl th{position:sticky;top:0;z-index:2;background:var(--ink2);font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);text-align:left;padding:9px 12px;font-weight:700;border-bottom:1px solid var(--rim)}
.ttbl td{padding:7px 12px;font-family:var(--mono);font-size:.74rem;border-bottom:1px solid var(--rim)60}
.ttbl tbody tr:hover td{background:var(--ink2)}
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:.63rem;font-weight:700}
.bw{background:var(--lime2);color:var(--lime)}.bl{background:var(--crimson2);color:var(--crimson)}
.bo{background:var(--violet2);color:var(--violet)}.be{background:var(--sky2);color:var(--sky)}
.bc{background:var(--teal2);color:var(--teal)}.bp{background:var(--gold2);color:var(--gold)}
.bov{background:var(--lime2);color:var(--lime)}.bun{background:var(--crimson2);color:var(--crimson)}
.bto{background:var(--sky2);color:var(--sky)}.bnt{background:var(--violet2);color:var(--violet)}

/* ── BOT CONTROL ── */
.ctrl-layout{display:grid;grid-template-columns:1fr 420px;gap:16px;align-items:start}
@media(max-width:960px){.ctrl-layout{grid-template-columns:1fr}}

.panel{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:18px 20px}
.panel h3{font-family:var(--font);font-size:.88rem;font-weight:800;color:var(--text);margin-bottom:16px;letter-spacing:-.02em}

.status-pill{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:var(--r);background:var(--ink2);border:1px solid var(--rim);margin-bottom:14px}
.sdot{width:12px;height:12px;border-radius:50%;flex-shrink:0;transition:all .3s}
.sdot.on{background:var(--lime);box-shadow:0 0 8px var(--lime);animation:blink 1.8s infinite}
.sdot.off{background:var(--crimson);box-shadow:0 0 6px var(--crimson)80}
.s-info .s-label{font-family:var(--font);font-size:.9rem;font-weight:700}
.s-info .s-sub{font-size:.68rem;color:var(--text3);margin-top:1px}

.mode-toggle{display:flex;border:1px solid var(--rim);border-radius:6px;overflow:hidden;margin-bottom:16px}
.mode-btn{flex:1;font-family:var(--font);font-size:.78rem;font-weight:700;padding:8px;border:none;background:var(--ink2);color:var(--text3);cursor:pointer;transition:all .15s}
.mode-btn.act-demo{background:var(--sky2);color:var(--sky)}
.mode-btn.act-real{background:var(--lime2);color:var(--lime)}

.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fg{display:flex;flex-direction:column;gap:4px}
.fg label{font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700}
.fg input,.fg select{font-family:var(--mono);font-size:.84rem;padding:8px 10px;border-radius:6px;border:1px solid var(--rim);background:var(--ink2);color:var(--text);outline:none;transition:border-color .15s;width:100%}
.fg input:focus,.fg select:focus{border-color:var(--lime)}
.fg .hint{font-size:.63rem;color:var(--text3)}
.fg.full{grid-column:1/-1}

.toggle-list{background:var(--ink2);border-radius:6px;padding:2px 12px;margin-top:12px}
.trow{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--rim)}
.trow:last-child{border-bottom:none}
.trow-info .tl{font-size:.8rem;font-weight:600;color:var(--text)}
.trow-info .ts{font-size:.66rem;color:var(--text3);margin-top:1px}
.sw{position:relative;width:38px;height:22px;flex-shrink:0}
.sw input{opacity:0;width:0;height:0}
.sw-slider{position:absolute;inset:0;background:var(--ink4);border:1px solid var(--rim);border-radius:11px;cursor:pointer;transition:.2s}
.sw-slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:var(--text3);border-radius:50%;transition:.2s}
.sw input:checked + .sw-slider{background:var(--lime2);border-color:var(--lime)}
.sw input:checked + .sw-slider::before{transform:translateX(16px);background:var(--lime)}

.sym-check-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.sym-chk-lbl{display:flex;align-items:center;gap:4px;padding:4px 9px;background:var(--ink2);border:1px solid var(--rim);border-radius:5px;cursor:pointer;font-family:var(--mono);font-size:.72rem;color:var(--text2);transition:all .15s}
.sym-chk-lbl:hover{border-color:var(--rim2);color:var(--text)}
.sym-chk-lbl input{accent-color:var(--lime)}
.sym-chk-lbl:has(input:checked){border-color:var(--lime)60;color:var(--lime);background:var(--lime2)}

.cmd-box{background:var(--ink);border:1px solid var(--rim);border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:.68rem;color:var(--text3);word-break:break-all;line-height:1.7;margin-top:12px;max-height:80px;overflow-y:auto}
.cmd-box .hl{color:var(--lime)}

/* ── SSE LOG ── */
.log-panel{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);display:flex;flex-direction:column;height:100%}
.log-panel-hd{padding:12px 16px;border-bottom:1px solid var(--rim);display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.log-panel-hd h3{font-family:var(--font);font-size:.8rem;font-weight:800;color:var(--text)}
.sse-indicator{display:flex;align-items:center;gap:5px;font-family:var(--mono);font-size:.65rem;color:var(--text3)}
.sse-dot{width:6px;height:6px;border-radius:50%;background:var(--text3)}
.sse-dot.conn{background:var(--lime);animation:blink 2s infinite}
.log-out{background:#050810;border-radius:0 0 var(--r2) var(--r2);padding:12px 14px;font-family:var(--mono);font-size:.7rem;color:#7fff80;line-height:1.7;flex:1;overflow-y:auto;white-space:pre-wrap;word-break:break-all;min-height:400px}
.log-out.muted{color:var(--text3);font-style:italic}
.log-out::-webkit-scrollbar{width:4px}
.log-out::-webkit-scrollbar-thumb{background:var(--rim)}

/* ── ML TAB ── */
.ml-meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:14px}
.ml-item{background:var(--ink2);border:1px solid var(--rim);border-radius:6px;padding:8px 10px}
.ml-item .mk{font-size:.59rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:3px}
.ml-item .mv{font-family:var(--mono);font-size:.82rem;font-weight:700}
.file-list{border:1px solid var(--rim);border-radius:6px;max-height:180px;overflow-y:auto;padding:3px}
.file-list::-webkit-scrollbar{width:4px}
.file-list::-webkit-scrollbar-thumb{background:var(--rim)}
.file-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:4px;cursor:pointer;font-size:.75rem}
.file-item:hover{background:var(--ink2)}
.file-item input{accent-color:var(--lime)}
.file-item .fn{font-family:var(--mono);color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-item .fm{font-family:var(--mono);font-size:.65rem;color:var(--text3)}
.ml-out{background:#050810;border-radius:6px;padding:12px;font-family:var(--mono);font-size:.68rem;color:#7fff80;line-height:1.7;max-height:400px;overflow-y:auto;white-space:pre-wrap;margin-top:12px}
.ml-out.muted{color:var(--text3);font-style:italic}
.thr-table{width:100%;border-collapse:collapse;margin-top:10px}
.thr-table th,.thr-table td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--rim);font-family:var(--mono);font-size:.72rem}
.thr-table th{text-align:right;font-size:.62rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);font-weight:700}
.thr-table th:first-child,.thr-table td:first-child{text-align:left}

/* ── SCANNER ── */
.scan-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;margin-top:14px}
.scan-card{background:var(--ink1);border:1px solid var(--rim);border-radius:var(--r2);padding:16px;transition:all .2s;position:relative;overflow:hidden}
.scan-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:transparent}
.scan-card:hover{transform:translateY(-2px);box-shadow:0 8px 24px #00000050}
.scan-card.strong{border-color:var(--lime)60}.scan-card.strong::before{background:var(--lime)}
.scan-card.good{border-color:var(--sky)60}.scan-card.good::before{background:var(--sky)}
.scan-card.wait{border-color:var(--gold)60}.scan-card.wait::before{background:var(--gold)}
.scan-card.danger{border-color:var(--crimson)60;opacity:.6}.scan-card.danger::before{background:var(--crimson)}
.scan-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.scan-sym{font-family:var(--mono);font-size:1rem;font-weight:700}
.sig-badge{font-size:.63rem;font-weight:700;padding:3px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em}
.sig-strong{background:var(--lime2);color:var(--lime)}
.sig-good{background:var(--sky2);color:var(--sky)}
.sig-wait{background:var(--gold2);color:var(--gold)}
.sig-no{background:var(--crimson2);color:var(--crimson)}
.scan-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.scan-m .sm-k{font-size:.58rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text3)}
.scan-m .sm-v{font-family:var(--mono);font-size:.78rem;font-weight:700}
.scan-pats{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.scan-pat{font-family:var(--mono);font-size:.66rem;padding:2px 7px;border-radius:4px;background:var(--ink3);border:1px solid var(--rim);color:var(--text3);transition:all .15s}
.scan-pat.on{border-color:var(--lime)60;color:var(--lime);background:var(--lime2)}
.scan-rec{background:var(--ink3);border-radius:6px;padding:9px 12px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.scan-rec-txt{font-size:.75rem;color:var(--text2);flex:1}
.scan-rec-txt strong{color:var(--text)}
.scan-use{font-family:var(--font);font-size:.7rem;font-weight:700;padding:5px 12px;border-radius:4px;border:1px solid var(--lime)60;background:var(--lime2);color:var(--lime);cursor:pointer;transition:all .15s;flex-shrink:0}
.scan-use:hover{background:var(--lime);color:var(--ink);transform:translateY(-1px)}
.trad-bar{height:4px;border-radius:2px;background:var(--ink3);overflow:hidden;margin-top:8px}
.trad-fill{height:100%;border-radius:2px;transition:width .4s}
.scan-footer{text-align:right;font-family:var(--mono);font-size:.62rem;color:var(--text3);margin-top:5px}

/* ── MISC ── */
.spin{width:16px;height:16px;border:2px solid var(--rim);border-top-color:var(--lime);border-radius:50%;animation:spin .7s linear infinite;display:inline-block;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-screen{position:fixed;inset:0;background:var(--ink);z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;transition:opacity .4s}
.loading-screen.done{opacity:0;pointer-events:none}
.loading-screen p{color:var(--text3);font-size:.85rem;font-family:var(--mono)}

::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--rim);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--rim2)}

@media(max-width:640px){
  .kpi-grid{grid-template-columns:repeat(2,1fr)}
  .fgrid{grid-template-columns:1fr}
  .ctrl-layout{grid-template-columns:1fr}
}
</style>
</head>
<body>

<div class="loading-screen" id="loader">
  <div class="spin" style="width:28px;height:28px;border-width:3px"></div>
  <p>Loading dashboard…</p>
</div>

<div class="wrap">

  <!-- TOPBAR -->
  <div class="topbar">
    <div class="logo">
      <div class="logo-mark">DB</div>
      <div>
        <div class="logo-text">DERIV BOT</div>
        <div class="logo-sub">Multi-Symbol Strategy Dashboard</div>
      </div>
    </div>
    <div class="topbar-right">
      <div class="proc-pills">
        <div class="proc-pill" id="pill-bbot"><span class="pip"></span> bbot</div>
        <div class="proc-pill" id="pill-daemon"><span class="pip"></span> daemon</div>
      </div>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.72rem;color:var(--text3)">
        <input type="checkbox" id="autoRefresh" style="accent-color:var(--lime)"> auto 10s
      </label>
      <button class="btn btn-ghost" onclick="refreshAll()">⟳ Refresh</button>
    </div>
  </div>

  <!-- TABS -->
  <div class="tabs">
    <button class="tab active" data-tab="analytics" onclick="switchTab('analytics')">
      <span class="tab-pip"></span> Analytics
    </button>
    <button class="tab" data-tab="control" onclick="switchTab('control')">
      <span class="tab-pip" id="tp-control"></span> Bot Control
    </button>
    <button class="tab" data-tab="training" onclick="switchTab('training')">
      <span class="tab-pip" id="tp-training"></span> ML Training
    </button>
    <button class="tab" data-tab="scanner" onclick="switchTab('scanner')">
      <span class="tab-pip"></span> Market Scanner
    </button>
  </div>

  <!-- ══════ ANALYTICS ══════ -->
  <div class="tab-content active" id="tab-analytics">

    <div class="section-label">Sessions <span id="sess-count" style="letter-spacing:normal;text-transform:none;font-weight:400;opacity:.5"></span></div>
    <div class="session-strip" id="sessionStrip"></div>

    <div id="empty-msg" class="empty-msg">
      <div class="em-icon">📊</div>
      <p>Select a session above to view detailed analytics, trade history, and symbol breakdown.</p>
    </div>

    <div id="dashboard" style="display:none">
      <div class="cfgbar" id="cfgBar"></div>
      <div class="kpi-grid" id="kpiGrid"></div>

      <!-- Equity + W/L donut -->
      <div class="chart-grid" style="margin-bottom:20px">
        <div class="chart-box">
          <div class="chart-title">Equity Curve</div>
          <canvas id="chartEquity" height="200"></canvas>
        </div>
        <div class="chart-box">
          <div class="chart-title">Win / Loss Split</div>
          <canvas id="chartWL" height="200"></canvas>
        </div>
      </div>

      <!-- Stake + result bar -->
      <div class="chart-box" style="margin-bottom:20px">
        <div class="chart-title">Stake per Trade</div>
        <canvas id="chartStake" height="120"></canvas>
      </div>

      <!-- Streak bar -->
      <div class="section-label">Streak Map</div>
      <div class="chart-box" style="margin-bottom:20px">
        <div class="streak-strip" id="streakBar"></div>
        <table style="width:100%;border-collapse:collapse;margin-top:8px">
          <thead><tr>
            <th style="font-size:.61rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);text-align:left;padding:6px 10px;border-bottom:1px solid var(--rim);font-weight:700">Type</th>
            <th style="font-size:.61rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);text-align:left;padding:6px 10px;border-bottom:1px solid var(--rim);font-weight:700">Length</th>
            <th style="font-size:.61rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);text-align:left;padding:6px 10px;border-bottom:1px solid var(--rim);font-weight:700">From</th>
            <th style="font-size:.61rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);text-align:left;padding:6px 10px;border-bottom:1px solid var(--rim);font-weight:700">To</th>
            <th style="font-size:.61rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);text-align:left;padding:6px 10px;border-bottom:1px solid var(--rim);font-weight:700">P&amp;L Impact</th>
          </tr></thead>
          <tbody id="streakBody"></tbody>
        </table>
      </div>

      <!-- Symbol breakdown -->
      <div class="section-label">Symbol Breakdown</div>
      <div class="sym-grid" id="symGrid"></div>

      <!-- Trade log -->
      <div class="section-label">Trade Log</div>
      <div class="trade-panel">
        <div class="trade-panel-hd">
          <h3>All Trades</h3>
          <span id="trade-count" style="font-family:var(--mono);font-size:.72rem;color:var(--text3)"></span>
        </div>
        <div class="trade-scroll">
          <table class="ttbl">
            <thead><tr>
              <th>#</th><th>Time</th><th>Symbol</th><th>Type</th><th>Result</th>
              <th>Stake</th><th>Profit</th><th>Payout</th><th>Cum P&amp;L</th><th>Equity</th>
            </tr></thead>
            <tbody id="tradeBody"></tbody>
          </table>
        </div>
      </div>
    </div>

  </div><!-- /analytics -->

  <!-- ══════ BOT CONTROL ══════ -->
  <div class="tab-content" id="tab-control">
    <div class="ctrl-layout">

      <!-- LEFT -->
      <div style="display:flex;flex-direction:column;gap:14px">

        <!-- Status -->
        <div class="panel" style="padding-bottom:14px">
          <div class="status-pill">
            <div class="sdot off" id="statusDot"></div>
            <div class="s-info">
              <div class="s-label" id="statusLabel">Checking…</div>
              <div class="s-sub">tmux · bbot</div>
            </div>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-red" id="stopBtn" onclick="stopBot()" disabled>⏹ Stop Bot</button>
          </div>
        </div>

        <!-- Config -->
        <div class="panel">
          <h3>⚙ Configure &amp; Start</h3>

          <div style="margin-bottom:14px">
            <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700;margin-bottom:6px">Account Mode</div>
            <div class="mode-toggle">
              <button class="mode-btn act-demo" id="modeDemo" onclick="setMode('demo')">Demo</button>
              <button class="mode-btn" id="modeReal" onclick="setMode('real')">Real</button>
            </div>
          </div>

          <div class="fgrid">
            <div class="fg full">
              <label>API Token</label>
              <input type="text" id="fToken" placeholder="Leave empty for default token" oninput="updateCmd()">
            </div>
            <div class="fg">
              <label>Base Stake (USD)</label>
              <input type="number" id="fStake" value="0.35" step="0.01" min="0.35" oninput="updateCmd()">
              <span class="hint">Min $0.35</span>
            </div>
            <div class="fg">
              <label>Martingale ×</label>
              <input type="number" id="fMartingale" value="2.2" step="0.1" min="1" oninput="updateCmd()">
            </div>
            <div class="fg">
              <label>Max Stake (USD)</label>
              <input type="number" id="fMaxStake" value="50" step="1" min="1" oninput="updateCmd()">
              <span class="hint" id="hMaxStake">Covers up to 7 losses</span>
            </div>
            <div class="fg">
              <label>Score Threshold</label>
              <input type="number" id="fThreshold" value="0.60" step="0.01" min="0" max="1" oninput="updateCmd()">
            </div>
            <div class="fg">
              <label>Algorithm</label>
              <select id="fStrategy" onchange="onStrategyChange();updateCmd()">
                <option value="alphabloom" selected>AlphaBloom</option>
                <option value="pulse">Pulse</option>
                <option value="ensemble">Ensemble</option>
                <option value="novaburst">NovaBurst</option>
                <option value="adaptive">Adaptive</option>
              </select>
            </div>
            <div class="fg">
              <label>Trade Strategy</label>
              <select id="fTradeStrategy" onchange="updateCmd()">
                <option value="even_odd" selected>Even/Odd</option>
                <option value="rise_fall_roll">Rise/Fall — Roll</option>
                <option value="rise_fall_zigzag">Rise/Fall — Zigzag</option>
                <option value="higher_lower_roll">Higher/Lower — Roll</option>
                <option value="higher_lower_zigzag">Higher/Lower — Zigzag</option>
                <option value="over_under_roll">Over/Under — Roll</option>
                <option value="touch_notouch_zigzag">Touch/NoTouch — Zigzag</option>
              </select>
            </div>
            <div class="fg" id="grp-mlThr" style="display:none">
              <label>ML Threshold</label>
              <input type="number" id="fMlThreshold" value="0.45" step="0.01" min="0" max="1" oninput="updateCmd()">
              <span class="hint">P(win) cutoff</span>
            </div>
            <div class="fg" id="grp-abWin">
              <label>AB Window (ticks)</label>
              <input type="number" id="fAbWindow" value="60" step="5" min="10" oninput="updateCmd()">
            </div>
            <div class="fg" id="grp-hotCold" style="display:none">
              <label>Hotness Cold Cutoff</label>
              <input type="number" id="fHotnessCold" value="0.43" step="0.01" oninput="updateCmd()">
            </div>
            <div class="fg" id="grp-hotProbe" style="display:none">
              <label>Hotness Probe</label>
              <input type="number" id="fHotnessProbe" value="20" step="1" oninput="updateCmd()">
            </div>
            <div class="fg" id="grp-mlIdle" style="display:none">
              <label>ML Idle Bypass (min)</label>
              <input type="number" id="fMlIdle" value="10" step="1" oninput="updateCmd()">
            </div>
            <div class="fg" id="grp-mlFloor" style="display:none">
              <label>ML Floor Threshold</label>
              <input type="number" id="fMlFloor" value="0.35" step="0.01" oninput="updateCmd()">
            </div>
            <div class="fg" id="grp-volSkip" style="display:none">
              <label>Vol Skip %ile</label>
              <input type="number" id="fVolSkip" value="0.75" step="0.05" oninput="updateCmd()">
            </div>
            <div class="fg">
              <label>Take Profit (USD)</label>
              <input type="number" id="fProfit" step="1" placeholder="optional" oninput="updateCmd()">
            </div>
            <div class="fg">
              <label>Loss Limit (USD)</label>
              <input type="number" id="fLoss" step="1" placeholder="optional e.g. -30" oninput="updateCmd()">
            </div>
          </div>

          <!-- Symbols -->
          <div style="margin-top:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
              <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700">Trade Symbols <span id="symCount" style="opacity:.6"></span></div>
              <div style="display:flex;gap:5px">
                <button type="button" class="btn btn-ghost" style="padding:3px 9px;font-size:.65rem" onclick="symAll(true)">All</button>
                <button type="button" class="btn btn-ghost" style="padding:3px 9px;font-size:.65rem" onclick="symAll(false)">None</button>
              </div>
            </div>
            <div class="sym-check-grid" id="symChecklist"></div>
          </div>

          <!-- Toggles -->
          <div class="toggle-list">
            <div class="trow">
              <div class="trow-info"><div class="tl">Disable Kelly Sizing</div><div class="ts">Base stake + martingale only</div></div>
              <label class="sw"><input type="checkbox" id="tKelly" checked onchange="updateCmd()"><span class="sw-slider"></span></label>
            </div>
            <div class="trow">
              <div class="trow-info"><div class="tl">Disable Risk Engine</div><div class="ts">No cooldown or circuit breaker</div></div>
              <label class="sw"><input type="checkbox" id="tRisk" onchange="updateCmd()"><span class="sw-slider"></span></label>
            </div>
            <div class="trow">
              <div class="trow-info"><div class="tl">ML Filter</div><div class="ts">Gate trades by P(win) model</div></div>
              <label class="sw"><input type="checkbox" id="tMl" onchange="syncMlVis();updateCmd()"><span class="sw-slider"></span></label>
            </div>
          </div>

          <div class="cmd-box" id="cmdBox"></div>

          <div style="margin-top:14px">
            <button class="btn btn-lime" id="startBtn" onclick="startBot()" style="width:100%">🚀 Start Bot</button>
          </div>
        </div>
      </div><!-- /left -->

      <!-- RIGHT: SSE Logs -->
      <div class="log-panel">
        <div class="log-panel-hd">
          <h3>Live Logs — bbot</h3>
          <div class="sse-indicator">
            <div class="sse-dot" id="sseDot"></div>
            <span id="sseLabel">disconnected</span>
          </div>
        </div>
        <div class="log-out muted" id="logOut">Bot is not running.</div>
      </div>

    </div>
  </div><!-- /control -->

  <!-- ══════ ML TRAINING ══════ -->
  <div class="tab-content" id="tab-training">
    <div class="ctrl-layout">
      <div style="display:flex;flex-direction:column;gap:14px">
        <!-- Model status -->
        <div class="panel">
          <h3>🧠 Model Status</h3>
          <div id="mlStatus" style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--ink2);border:1px solid var(--rim);border-radius:6px;margin-bottom:12px">
            <div style="width:10px;height:10px;border-radius:50%;background:var(--crimson);flex-shrink:0" id="mlDot2"></div>
            <div id="mlStatusTxt" style="font-family:var(--mono);font-size:.75rem">Checking…</div>
          </div>
          <div class="ml-meta-grid" id="mlMetaGrid" style="display:none"></div>
          <div id="mlThrWrap" style="display:none">
            <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700;margin-top:10px;margin-bottom:6px">Threshold Performance</div>
            <table class="thr-table" id="mlThrTable"></table>
          </div>
        </div>

        <!-- Train -->
        <div class="panel">
          <h3>⚙ Train New Model</h3>
          <div class="fgrid">
            <div class="fg">
              <label>Model Type</label>
              <select id="fMlModel">
                <option value="logreg" selected>Logistic Regression</option>
                <option value="gbm">Gradient Boosting</option>
              </select>
            </div>
            <div class="fg">
              <label>Threshold</label>
              <input type="number" id="fMlThr" value="0.50" step="0.01" min="0" max="1">
            </div>
            <div class="fg">
              <label>Test Fraction</label>
              <input type="number" id="fMlFrac" value="0.20" step="0.05" min="0.05" max="0.5">
            </div>
            <div class="fg">
              <label>Min Trades</label>
              <input type="number" id="fMlMin" value="200" step="50" min="50">
            </div>
            <div class="fg">
              <label>History Weight</label>
              <input type="number" id="fMlHist" value="0.50" step="0.1" min="0.1" max="2">
            </div>
          </div>
          <div class="toggle-list" style="margin-top:10px">
            <div class="trow">
              <div class="trow-info"><div class="tl">Exclude Historical Data</div><div class="ts">Skip history-trades.json</div></div>
              <label class="sw"><input type="checkbox" id="tNoHist"><span class="sw-slider"></span></label>
            </div>
          </div>
          <div style="margin-top:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
              <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700">Training Data <span id="mlFileCount" style="opacity:.5"></span></div>
              <div style="display:flex;gap:5px">
                <button type="button" class="btn btn-ghost" style="padding:3px 9px;font-size:.65rem" onclick="mlSelAll(true)">All</button>
                <button type="button" class="btn btn-ghost" style="padding:3px 9px;font-size:.65rem" onclick="mlSelAll(false)">None</button>
                <button type="button" class="btn btn-ghost" style="padding:3px 9px;font-size:.65rem" onclick="loadMlFiles()">⟳</button>
              </div>
            </div>
            <div class="file-list" id="mlFileList"><div style="padding:10px;color:var(--text3);font-size:.75rem">Loading…</div></div>
          </div>
          <div style="margin-top:14px">
            <button class="btn btn-lime" id="mlTrainBtn" onclick="trainModel()" style="width:100%">🔬 Train Model</button>
          </div>

          <!-- Fetch History -->
          <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--rim)">
            <h3>📡 Fetch Historical Ticks</h3>
            <p style="font-size:.75rem;color:var(--text3);margin:0 0 12px;line-height:1.6">Download tick history from Deriv API and simulate strategy outcomes for training data.</p>
            <div class="fgrid">
              <div class="fg">
                <label>Hours of History</label>
                <input type="number" id="fHistHours" value="48" step="12" min="1" max="720">
              </div>
              <div class="fg">
                <label>App ID</label>
                <input type="number" id="fHistAppId" value="1089" step="1" min="1">
              </div>
            </div>
            <div style="margin-top:10px">
              <button class="btn btn-ghost" id="fetchHistBtn" onclick="fetchHistory()" style="width:100%">📡 Fetch &amp; Simulate</button>
            </div>
            <div class="ml-out muted" id="fetchHistOut" style="display:none;margin-top:10px"></div>
          </div>
        </div>
      </div>

      <!-- RIGHT -->
      <div class="log-panel">
        <div class="log-panel-hd">
          <h3>Training Output</h3>
          <button class="btn btn-ghost" onclick="refreshMlStatus()" style="padding:4px 10px;font-size:.7rem">⟳ Refresh</button>
        </div>
        <div class="ml-out muted" id="mlOut" style="border-radius:0 0 var(--r2) var(--r2);flex:1;min-height:400px">No training run yet.</div>
      </div>
    </div>
  </div><!-- /training -->

  <!-- ══════ SCANNER ══════ -->
  <div class="tab-content" id="tab-scanner">
    <!-- Daemon -->
    <div class="panel" style="margin-bottom:12px">
      <h3>📡 Background Market Daemon</h3>
      <p style="font-size:.75rem;color:var(--text3);margin:0 0 12px;line-height:1.6">Subscribes to live tick streams and continuously updates analysis. Results are instant compared to re-fetching history.</p>
      <div class="fgrid" style="max-width:480px">
        <div class="fg">
          <label>Snapshot Interval</label>
          <select id="fDaemonInterval">
            <option value="10">Every 10s</option>
            <option value="15">Every 15s</option>
            <option value="30" selected>Every 30s</option>
            <option value="60">Every 1min</option>
          </select>
        </div>
        <div class="fg">
          <label>App ID</label>
          <input type="number" id="fDaemonAppId" value="1089">
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-lime" id="daemonStartBtn" onclick="startDaemon()">▶ Start Daemon</button>
        <button class="btn btn-red" id="daemonStopBtn" onclick="stopDaemon()" style="display:none">⏹ Stop Daemon</button>
        <div style="display:flex;align-items:center;gap:6px">
          <div class="sdot off" id="daemonDot"></div>
          <span style="font-size:.72rem;color:var(--text3)" id="daemonLabel">Stopped</span>
        </div>
      </div>
    </div>

    <!-- Scan controls -->
    <div class="panel" style="margin-bottom:12px">
      <h3>🔍 Scan Configuration</h3>
      <div class="fgrid" style="max-width:640px">
        <div class="fg">
          <label>Auto-Refresh</label>
          <select id="fScanInterval">
            <option value="0">Off</option>
            <option value="0.5">30s</option>
            <option value="1" selected>1 min</option>
            <option value="2">2 min</option>
            <option value="5">5 min</option>
          </select>
        </div>
        <div class="fg">
          <label>Lookback (manual)</label>
          <select id="fScanHours">
            <option value="0.25">15 min</option>
            <option value="0.5">30 min</option>
            <option value="1" selected>1 hour</option>
            <option value="2">2 hours</option>
            <option value="4">4 hours</option>
          </select>
        </div>
        <div class="fg">
          <label>App ID</label>
          <input type="number" id="fScanAppId" value="1089">
        </div>
      </div>

      <div style="margin-top:12px">
        <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700;margin-bottom:6px">Allowed Algorithms</div>
        <div style="display:flex;flex-wrap:wrap;gap:5px" id="algoFilter">
          <label class="sym-chk-lbl"><input type="checkbox" class="algoFilterChk" value="pulse" checked> Pulse</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="algoFilterChk" value="alphabloom" checked> AlphaBloom</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="algoFilterChk" value="ensemble" checked> Ensemble</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="algoFilterChk" value="novaburst" checked> NovaBurst</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="algoFilterChk" value="adaptive" checked> Adaptive</label>
        </div>
      </div>
      <div style="margin-top:10px">
        <div style="font-size:.63rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);font-weight:700;margin-bottom:6px">Allowed Trade Strategies</div>
        <div style="display:flex;flex-wrap:wrap;gap:5px" id="tsFilter">
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="even_odd" checked> Even/Odd</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="rise_fall_roll" checked> Rise/Fall Roll</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="rise_fall_zigzag" checked> Rise/Fall Zigzag</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="higher_lower_roll"> Higher/Lower Roll</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="higher_lower_zigzag"> Higher/Lower Zigzag</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="over_under_roll"> Over/Under Roll</label>
          <label class="sym-chk-lbl"><input type="checkbox" class="tsFilterChk" value="touch_notouch_zigzag"> Touch/NoTouch Zigzag</label>
        </div>
      </div>

      <div class="toggle-list" style="max-width:640px;margin-top:12px">
        <div class="trow">
          <div class="trow-info"><div class="tl">Auto-Exclude DO_NOT_ENTER</div><div class="ts">Uncheck unsafe symbols in Bot Control</div></div>
          <label class="sw"><input type="checkbox" id="tAutoExclude" checked><span class="sw-slider"></span></label>
        </div>
        <div class="trow">
          <div class="trow-info"><div class="tl">Auto-Apply Best Strategy</div><div class="ts">Set algo + trade strategy from top result</div></div>
          <label class="sw"><input type="checkbox" id="tAutoStrategy"><span class="sw-slider"></span></label>
        </div>
      </div>

      <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-lime" id="scanBtn" onclick="startScan()">🔍 Manual Scan</button>
        <button class="btn btn-red" id="scanStopBtn" onclick="stopScan()" style="display:none">⏹ Stop</button>
        <button class="btn btn-sky" onclick="refreshFromDaemon()">🔄 From Daemon</button>
        <button class="btn btn-ghost" id="autoScanBtn" onclick="toggleAutoScan()">↻ Start Auto-Refresh</button>
        <div id="scanProgress" style="display:none;align-items:center;gap:8px;font-family:var(--mono);font-size:.72rem;color:var(--text3)">
          <div class="spin" style="width:12px;height:12px;border-width:2px"></div>
          <span id="scanProgressTxt">Connecting…</span>
          <div style="width:160px;height:4px;background:var(--ink3);border-radius:2px;overflow:hidden">
            <div id="scanProgressFill" style="height:100%;background:linear-gradient(90deg,var(--lime),var(--teal));border-radius:2px;width:0%;transition:width .3s"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Results -->
    <div id="scanResultsWrap" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
        <div class="section-label" style="margin:0">Scan Results <span id="scanResultCount" style="letter-spacing:normal;text-transform:none;font-weight:400;opacity:.5"></span></div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn btn-ghost" onclick="applyAllScanResults()" style="font-size:.72rem;padding:5px 12px">✅ Apply All to Bot</button>
          <span style="font-family:var(--mono);font-size:.65rem;color:var(--text3)" id="scanTimestamp"></span>
        </div>
      </div>
      <div class="scan-results" id="scanResults"></div>
    </div>
  </div><!-- /scanner -->

</div><!-- /wrap -->

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
// ══════════════════════════════════════════════════════════════
// STATE
// ══════════════════════════════════════════════════════════════
let sessions = [], activeFile = null, activeData = null;
let charts = {}, autoTimer = null, botRunning = false;
let currentMode = 'demo';
let sseSource = null;  // SSE for bot logs
let scanSource = null, scanResults = [];
let autoScanTimer = null, autoScanRunning = false;
let procPollTimer = null;
const ALL_SYMS = ['R_10','R_25','R_50','R_75','R_100','1HZ10V','1HZ25V','1HZ50V','1HZ75V','1HZ100V'];

// ══════════════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════════════
async function init() {
  initSymChecklist();
  onStrategyChange();
  updateCmd();
  await loadSessions();
  startProcPoll();
  document.getElementById('loader').classList.add('done');
}

// ══════════════════════════════════════════════════════════════
// TABS
// ══════════════════════════════════════════════════════════════
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-'+tab));
  if (tab === 'control') { connectSseLogs(); refreshBotStatus(); }
  if (tab === 'training') { refreshMlStatus(); loadMlFiles(); }
  if (tab !== 'control') disconnectSseLogs();
}

// ══════════════════════════════════════════════════════════════
// API
// ══════════════════════════════════════════════════════════════
async function api(url, opts) {
  const r = await fetch(url, { cache:'no-store', ...opts });
  if (!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
}
async function post(url, body) {
  return api(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
}

// ══════════════════════════════════════════════════════════════
// BACKGROUND PROCESS PILLS
// ══════════════════════════════════════════════════════════════
function startProcPoll() {
  pollProcs();
  procPollTimer = setInterval(pollProcs, 8000);
}
async function pollProcs() {
  try {
    const d = await api('?api=process_status');
    setPill('pill-bbot', d.sessions.bbot);
    setPill('pill-daemon', d.sessions.market_daemon);
  } catch(e) {}
}
function setPill(id, active) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('active', active);
}

// ══════════════════════════════════════════════════════════════
// SESSIONS
// ══════════════════════════════════════════════════════════════
async function loadSessions() {
  try {
    sessions = await api('?api=sessions');
    renderStrip();
    if (activeFile && sessions.find(s => s.file === activeFile)) {
      await loadSession(activeFile, false);
    }
  } catch(e) { console.error(e); }
}

function refreshAll() {
  loadSessions();
  if (document.getElementById('tab-control').classList.contains('active')) refreshBotStatus();
  pollProcs();
}

document.getElementById('autoRefresh').addEventListener('change', function() {
  clearInterval(autoTimer);
  if (this.checked) autoTimer = setInterval(refreshAll, 10000);
});

function renderStrip() {
  const strip = document.getElementById('sessionStrip');
  const cnt = document.getElementById('sess-count');
  cnt.textContent = sessions.length ? `(${sessions.length})` : '';
  if (!sessions.length) {
    strip.innerHTML = '<div style="color:var(--text3);padding:20px;font-size:.8rem;font-family:var(--mono)">No JSON files in data/</div>';
    return;
  }
  strip.innerHTML = sessions.map(s => {
    const pnl = s.net_pnl ?? 0;
    const pc = pnl >= 0 ? 'g' : 'r';
    const wr = ((s.win_rate ?? 0)*100).toFixed(1);
    const badge = `<span class="mode-badge ${s.account_mode==='live'?'live':'demo'}">${s.account_mode}</span>`;
    return `<div class="scard ${s.is_live?'live':''} ${activeFile===s.file?'sel':''}"
              data-file="${s.file}" onclick="loadSession('${s.file}')">
      <div class="sc-id">${s.file.replace('.json','')}</div>
      <div class="sc-meta">${badge} ${fmtTs(s.started_at)}</div>
      <div class="sc-row">
        <div class="sc-stat"><span class="k">Trades</span><span class="v">${s.trade_count}</span></div>
        <div class="sc-stat"><span class="k">Win%</span><span class="v">${wr}%</span></div>
        <div class="sc-stat"><span class="k">W/L</span><span class="v">${s.wins}/${s.losses}</span></div>
        <div class="sc-stat"><span class="k">P&amp;L</span><span class="v ${pc}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span></div>
      </div>
    </div>`;
  }).join('');
}

async function loadSession(file, scroll=true) {
  activeFile = file;
  try {
    activeData = await api('?api=session&file='+encodeURIComponent(file));
    renderDash();
    document.getElementById('empty-msg').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    document.querySelectorAll('.scard').forEach(c => c.classList.toggle('sel', c.dataset.file === file));
    if (scroll) document.getElementById('dashboard').scrollIntoView({behavior:'smooth',block:'start'});
  } catch(e) { console.error(e); }
}

// ══════════════════════════════════════════════════════════════
// DASHBOARD
// ══════════════════════════════════════════════════════════════
function renderDash() {
  if (!activeData) return;
  const d = activeData, sess = d.session||{}, sum = d.summary||{};
  const trades = d.trades||[], curve = d.equity_curve||[];

  // Compute authoritative values from trades array directly
  const tradeCount = trades.length;
  const wins = trades.filter(t => t.result === 'win').length;
  const losses = trades.filter(t => t.result === 'loss').length;
  const netPnl = trades.reduce((a, t) => a + (t.profit||0), 0);
  const winRate = tradeCount > 0 ? (wins/tradeCount*100) : 0;

  const initEq = sess.initial_equity||0;
  const curEq = sess.current_equity || (initEq + netPnl);

  // Peak & drawdown from equity curve
  let peak = initEq, maxDD = 0;
  for (const pt of curve) {
    if (pt.equity > peak) peak = pt.equity;
    const dd = peak - pt.equity;
    if (dd > maxDD) maxDD = dd;
  }

  const winT = trades.filter(t=>t.result==='win');
  const lossT = trades.filter(t=>t.result==='loss');
  const avgW = winT.length ? winT.reduce((a,t)=>a+t.profit,0)/winT.length : 0;
  const avgL = lossT.length ? lossT.reduce((a,t)=>a+Math.abs(t.profit),0)/lossT.length : 0;

  const streaks = computeStreaks(trades);
  const maxWS = Math.max(0,...streaks.filter(s=>s.type==='win').map(s=>s.length));
  const maxLS = Math.max(0,...streaks.filter(s=>s.type==='loss').map(s=>s.length));
  const dur = computeDuration(sess.started_at, sess.updated_at);
  const eqDiff = curEq - initEq;

  // Config bar
  document.getElementById('cfgBar').innerHTML = `
    <div class="cfg-item"><span class="ck">Account</span><span class="cv">${sess.account_loginid||'—'}</span></div>
    <div class="cfg-item"><span class="ck">Mode</span><span class="cv" style="color:${sess.account_mode==='live'?'var(--lime)':'var(--sky)'};text-transform:capitalize">${sess.account_mode||'demo'}</span></div>
    <div class="cfg-item"><span class="ck">Base Stake</span><span class="cv">$${sess.base_stake??'—'}</span></div>
    <div class="cfg-item"><span class="ck">Score Thr</span><span class="cv">${sess.score_threshold??'—'}</span></div>
    <div class="cfg-item"><span class="ck">Profit Target</span><span class="cv" style="color:var(--lime)">${sess.profit_target!=null?'$'+sess.profit_target:'∞'}</span></div>
    <div class="cfg-item"><span class="ck">Loss Limit</span><span class="cv" style="color:var(--crimson)">${sess.loss_limit!=null?'$'+sess.loss_limit:'∞'}</span></div>
    <div class="cfg-item"><span class="ck">Started</span><span class="cv" style="font-size:.72rem">${fmtTs(sess.started_at)}</span></div>
    <div class="cfg-item"><span class="ck">Duration</span><span class="cv">${dur}</span></div>
    <div class="cfg-item"><span class="ck">Symbols</span><span class="cv"><div class="sym-tags">${(sess.symbols||[]).map(s=>`<span class="sym-tag">${s}</span>`).join('')}</div></span></div>
  `;

  // KPI cards
  const pnlCls = netPnl>=0?'hl-g':'hl-r';
  const eqCls = eqDiff>=0?'g':'r';
  document.getElementById('kpiGrid').innerHTML = `
    <div class="kpi hl ${pnlCls}"><div class="kpi-label">Net P&amp;L</div><div class="kpi-val ${netPnl>=0?'g':'r'}">${netPnl>=0?'+':''}$${netPnl.toFixed(2)}</div><div class="kpi-sub">${tradeCount} trades</div></div>
    <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-val">${winRate.toFixed(1)}%</div><div class="kpi-sub">${wins}W / ${losses}L</div></div>
    <div class="kpi"><div class="kpi-label">Equity</div><div class="kpi-val">$${curEq.toFixed(2)}</div><div class="kpi-sub ${eqCls}">${eqDiff>=0?'+':''}$${eqDiff.toFixed(2)}</div></div>
    <div class="kpi hl hl-g"><div class="kpi-label">Peak Equity</div><div class="kpi-val g">$${peak.toFixed(2)}</div><div class="kpi-sub">+$${(peak-initEq).toFixed(2)}</div></div>
    <div class="kpi hl hl-r"><div class="kpi-label">Max Drawdown</div><div class="kpi-val r">-$${maxDD.toFixed(2)}</div></div>
    <div class="kpi"><div class="kpi-label">Avg Win / Loss</div><div class="kpi-val" style="font-size:1rem"><span class="g">$${avgW.toFixed(2)}</span><span style="color:var(--text3)"> / </span><span class="r">$${avgL.toFixed(2)}</span></div></div>
    <div class="kpi"><div class="kpi-label">Best/Worst Streak</div><div class="kpi-val" style="font-size:1rem"><span class="g">${maxWS}W</span><span style="color:var(--text3)"> / </span><span class="r">${maxLS}L</span></div></div>
    <div class="kpi"><div class="kpi-label">Duration</div><div class="kpi-val" style="font-size:1rem">${dur}</div></div>
  `;

  renderCharts(trades, curve);
  renderStreaks(streaks, trades);
  renderSymGrid(trades);

  // Trade table
  document.getElementById('trade-count').textContent = tradeCount+' trades';
  let cum = 0;
  document.getElementById('tradeBody').innerHTML = trades.map(t => {
    cum += t.profit;
    const win = t.result==='win';
    const ps = t.profit>=0?'+':'', cs = cum>=0?'+':'';
    return `<tr>
      <td>${t.trade_no}</td>
      <td style="font-size:.68rem">${fmtTs(t.timestamp)}</td>
      <td><span class="sym-tag">${t.symbol}</span></td>
      <td>${contractBadge(t.contract_type)}</td>
      <td><span class="badge ${win?'bw':'bl'}">${t.result.toUpperCase()}</span></td>
      <td>$${t.stake.toFixed(2)}</td>
      <td class="${win?'g':'r'}">${ps}$${t.profit.toFixed(2)}</td>
      <td>$${t.payout.toFixed(2)}</td>
      <td style="color:${cum>=0?'var(--lime)':'var(--crimson)'}">${cs}$${cum.toFixed(2)}</td>
      <td>$${t.equity_after.toFixed(2)}</td>
    </tr>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════
// CHARTS
// ══════════════════════════════════════════════════════════════
function destroyCharts(){Object.values(charts).forEach(c=>c.destroy());charts={}}
const TICK='#4d607e', MF='Space Mono';
function baseOpts(){return{responsive:true,maintainAspectRatio:true,animation:{duration:600},plugins:{legend:{display:false},tooltip:{titleColor:'#dce6f5',bodyColor:'#dce6f5',backgroundColor:'#1a2236ee',borderColor:'#3d5070',borderWidth:1,titleFont:{family:MF,size:10},bodyFont:{family:MF,size:10}}},scales:{x:{grid:{color:'#1a223680'},ticks:{color:TICK,font:{family:MF,size:9},maxTicksLimit:12}},y:{grid:{color:'#1a223680'},ticks:{color:TICK,font:{family:MF,size:9}}}}}}

function renderCharts(trades, curve) {
  destroyCharts();
  if (!trades.length) return;

  const labels = trades.map(t => '#'+t.trade_no);
  const ptC = trades.map(t => t.result==='win'?'#a8f03d':'#f0503d');

  // EQUITY CURVE
  const eqCtx = document.getElementById('chartEquity').getContext('2d');
  const eqG = eqCtx.createLinearGradient(0,0,0,220);
  eqG.addColorStop(0,'#3df0d430'); eqG.addColorStop(1,'#3df0d400');
  charts.eq = new Chart(eqCtx, {
    type:'line',
    data:{labels:curve.map(p=>'#'+p.trade_no), datasets:[{
      data:curve.map(p=>p.equity),
      borderColor:'#3df0d4', backgroundColor:eqG,
      fill:true, tension:.3,
      pointRadius:curve.length>80?0:3,
      pointBackgroundColor:'#3df0d4',
      borderWidth:2
    }]},
    options:{...baseOpts(), scales:{
      x:{grid:{color:'#1a223680'},ticks:{color:TICK,font:{family:MF,size:9},maxTicksLimit:10}},
      y:{grid:{color:'#1a223680'},ticks:{color:TICK,font:{family:MF,size:9},callback:v=>'$'+v.toFixed(0)}}
    }}
  });

  // W/L DONUT
  const wl = trades.filter(t=>t.result==='win').length;
  charts.wl = new Chart(document.getElementById('chartWL').getContext('2d'), {
    type:'doughnut',
    data:{labels:['Wins','Losses'],datasets:[{
      data:[wl,trades.length-wl],
      backgroundColor:['#a8f03d90','#f0503d90'],
      borderColor:['#a8f03d','#f0503d'],
      borderWidth:2, hoverOffset:4
    }]},
    options:{responsive:true,maintainAspectRatio:true,cutout:'68%',animation:{duration:600},
      plugins:{legend:{position:'bottom',labels:{color:TICK,font:{family:'DM Sans',size:11},padding:14}}}}
  });

  // STAKE BAR
  charts.stake = new Chart(document.getElementById('chartStake').getContext('2d'), {
    type:'bar',
    data:{labels, datasets:[{
      data:trades.map(t=>t.stake),
      backgroundColor:ptC.map(c=>c+'70'),
      borderColor:ptC, borderWidth:1, borderRadius:2
    }]},
    options:{...baseOpts(), scales:{
      x:{grid:{color:'#1a223640'},ticks:{color:TICK,font:{family:MF,size:9},maxTicksLimit:16}},
      y:{grid:{color:'#1a223640'},ticks:{color:TICK,font:{family:MF,size:9},callback:v=>'$'+v.toFixed(2)}}
    }}
  });
}

// ══════════════════════════════════════════════════════════════
// STREAKS
// ══════════════════════════════════════════════════════════════
function computeStreaks(trades) {
  if (!trades.length) return [];
  const out=[];
  let cur={type:trades[0].result,start:0,length:1,pnl:trades[0].profit};
  for(let i=1;i<trades.length;i++){
    if(trades[i].result===cur.type){cur.length++;cur.pnl+=trades[i].profit}
    else{cur.end=i-1;out.push({...cur});cur={type:trades[i].result,start:i,length:1,pnl:trades[i].profit}}
  }
  cur.end=trades.length-1; out.push(cur); return out;
}

function renderStreaks(streaks, trades) {
  const total = trades.length||1;
  document.getElementById('streakBar').innerHTML = streaks.map(sk => {
    const pct = (sk.length/total)*100;
    const sign = sk.pnl>=0?'+':'';
    return `<div class="streak-seg ${sk.type==='win'?'w':'l'}" style="width:${Math.max(pct,1.2)}%">
      ${sk.length>2?sk.length:''}
      <div class="streak-tip">${sk.type.toUpperCase()} ×${sk.length}<br>${sign}$${sk.pnl.toFixed(2)}<br>#${sk.start+1}–#${sk.end+1}</div>
    </div>`;
  }).join('');

  const notable = streaks.filter(s=>s.length>=2).sort((a,b)=>b.length-a.length);
  document.getElementById('streakBody').innerHTML = notable.length
    ? notable.map(sk => {
        const sign = sk.pnl>=0?'+':'';
        return `<tr>
          <td style="padding:7px 10px;font-family:var(--mono);font-size:.72rem;border-bottom:1px solid var(--rim)60"><span class="badge ${sk.type==='win'?'bw':'bl'}">${sk.type.toUpperCase()}</span></td>
          <td style="padding:7px 10px;font-family:var(--mono);font-size:.72rem;border-bottom:1px solid var(--rim)60">${sk.length} in a row</td>
          <td style="padding:7px 10px;font-family:var(--mono);font-size:.72rem;border-bottom:1px solid var(--rim)60">#${sk.start+1}</td>
          <td style="padding:7px 10px;font-family:var(--mono);font-size:.72rem;border-bottom:1px solid var(--rim)60">#${sk.end+1}</td>
          <td style="padding:7px 10px;font-family:var(--mono);font-size:.72rem;border-bottom:1px solid var(--rim)60;color:${sk.pnl>=0?'var(--lime)':'var(--crimson)'}">${sign}$${sk.pnl.toFixed(2)}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="5" style="padding:14px;color:var(--text3);text-align:center">No streaks of 2+ yet</td></tr>';
}

// ══════════════════════════════════════════════════════════════
// SYMBOL GRID
// ══════════════════════════════════════════════════════════════
function renderSymGrid(trades) {
  const map = {};
  for(const t of trades){
    if(!map[t.symbol]) map[t.symbol]={wins:0,losses:0,pnl:0};
    if(t.result==='win') map[t.symbol].wins++; else map[t.symbol].losses++;
    map[t.symbol].pnl += t.profit;
  }
  document.getElementById('symGrid').innerHTML = Object.entries(map)
    .sort((a,b)=>(b[1].wins+b[1].losses)-(a[1].wins+a[1].losses))
    .map(([sym,s]) => {
      const tot = s.wins+s.losses, wr = tot?(s.wins/tot*100):0;
      const pc = s.pnl>=0?'g':'r'; const sign = s.pnl>=0?'+':'';
      return `<div class="sym-card">
        <div class="sym-name">${sym}</div>
        <div class="sym-row">
          <div class="sym-stat"><span class="k">Trades</span><span class="v">${tot}</span></div>
          <div class="sym-stat"><span class="k">W/L</span><span class="v">${s.wins}/${s.losses}</span></div>
          <div class="sym-stat"><span class="k">Win%</span><span class="v">${wr.toFixed(0)}%</span></div>
          <div class="sym-stat"><span class="k">P&amp;L</span><span class="v ${pc}">${sign}$${s.pnl.toFixed(2)}</span></div>
        </div>
        <div class="sym-bar"><div class="sym-bar-fill" style="width:${wr}%;background:${wr>=50?'var(--lime)':'var(--crimson)'}"></div></div>
      </div>`;
    }).join('');
}

// ══════════════════════════════════════════════════════════════
// BOT CONTROL
// ══════════════════════════════════════════════════════════════
function setMode(m) {
  currentMode = m;
  document.getElementById('modeDemo').className = 'mode-btn'+(m==='demo'?' act-demo':'');
  document.getElementById('modeReal').className = 'mode-btn'+(m==='real'?' act-real':'');
  updateCmd();
}

function onStrategyChange() {
  const s = document.getElementById('fStrategy').value;
  const isAdaptive = s==='adaptive';
  document.getElementById('grp-abWin').style.display = s==='alphabloom'?'':'none';
  document.getElementById('grp-hotCold').style.display = isAdaptive?'':'none';
  document.getElementById('grp-hotProbe').style.display = isAdaptive?'':'none';
  document.getElementById('grp-mlIdle').style.display = isAdaptive?'':'none';
  document.getElementById('grp-mlFloor').style.display = isAdaptive?'':'none';
  document.getElementById('grp-volSkip').style.display = isAdaptive?'':'none';
  syncMlVis();
}
function syncMlVis() {
  const s = document.getElementById('fStrategy').value;
  const mlOn = document.getElementById('tMl').checked;
  document.getElementById('grp-mlThr').style.display = (s==='adaptive'||mlOn)?'':'none';
}

function buildParams() {
  const token = document.getElementById('fToken').value.trim()||'gY5gbEpJVhih5NL';
  const stake = parseFloat(document.getElementById('fStake').value)||0.35;
  const mart  = parseFloat(document.getElementById('fMartingale').value)||2.2;
  const maxSt = parseFloat(document.getElementById('fMaxStake').value)||50;
  const thr   = parseFloat(document.getElementById('fThreshold').value)||0.60;
  const strat = document.getElementById('fStrategy').value;
  const ts    = document.getElementById('fTradeStrategy').value;
  const abWin = parseInt(document.getElementById('fAbWindow').value)||60;
  const disK  = document.getElementById('tKelly').checked;
  const disR  = document.getElementById('tRisk').checked;
  const mlOn  = document.getElementById('tMl').checked;
  const mlThr = parseFloat(document.getElementById('fMlThreshold').value);
  const hotC  = parseFloat(document.getElementById('fHotnessCold').value)||0.43;
  const hotP  = parseInt(document.getElementById('fHotnessProbe').value)||20;
  const mlI   = parseFloat(document.getElementById('fMlIdle').value)||10;
  const mlF   = parseFloat(document.getElementById('fMlFloor').value)||0.35;
  const volS  = parseFloat(document.getElementById('fVolSkip').value)||0.75;
  const profRaw = document.getElementById('fProfit').value.trim();
  const lossRaw = document.getElementById('fLoss').value.trim();
  return {
    token, mode:currentMode, base_stake:stake, martingale:mart, max_stake:maxSt,
    threshold:thr, strategy:strat, trade_strategy:ts, ab_window:abWin,
    disable_kelly:disK, disable_risk:disR, ml_filter:mlOn,
    ml_threshold:isNaN(mlThr)?null:mlThr,
    hotness_cold:hotC, hotness_probe:hotP, ml_idle_minutes:mlI, ml_floor:mlF, vol_skip_pct:volS,
    profit_target:profRaw!==''?parseFloat(profRaw):null,
    loss_limit:lossRaw!==''?parseFloat(lossRaw):null,
    symbols:getSelSyms()
  };
}

function updateCmd() {
  const p = buildParams();
  const tok = p.token.length>5
    ? p.token.slice(0,3)+'•'.repeat(Math.max(0,p.token.length-5))+p.token.slice(-2)
    : p.token;
  const levels = p.martingale>1 ? Math.floor(Math.log(p.max_stake/p.base_stake)/Math.log(p.martingale)) : '∞';
  const hEl = document.getElementById('hMaxStake');
  if(hEl) hEl.textContent = `Covers ~${levels} consecutive losses`;
  syncMlVis();

  let cmd = `python3 bot.py --token <span class="hl">${tok}</span> --account-mode ${p.mode}`;
  cmd += ` --base-stake ${p.base_stake.toFixed(2)} --martingale ${p.martingale.toFixed(1)} --max-stake ${p.max_stake.toFixed(0)}`;
  cmd += ` --score-threshold ${p.threshold.toFixed(2)} --strategy ${p.strategy} --trade-strategy ${p.trade_strategy}`;
  if(p.strategy==='alphabloom') cmd += ` --ab-window ${p.ab_window}`;
  if(p.disable_kelly) cmd += ' --disable-kelly';
  if(p.disable_risk) cmd += ' --disable-risk-engine';
  if(p.ml_filter && p.strategy!=='adaptive') cmd += ' --ml-filter';
  if((p.ml_filter||p.strategy==='adaptive') && p.ml_threshold!==null) cmd += ` --ml-threshold ${p.ml_threshold.toFixed(2)}`;
  if(p.strategy==='adaptive') cmd += ` --hotness-cold ${p.hotness_cold.toFixed(2)} --hotness-probe ${p.hotness_probe} --vol-skip-pct ${p.vol_skip_pct.toFixed(2)} --ml-idle-minutes ${p.ml_idle_minutes.toFixed(0)} --ml-floor ${p.ml_floor.toFixed(2)}`;
  if(p.profit_target!==null) cmd += ` --profit-target ${p.profit_target}`;
  if(p.loss_limit!==null) cmd += ` --loss-limit ${p.loss_limit}`;
  if(p.symbols && p.symbols.length>0 && p.symbols.length<ALL_SYMS.length) cmd += ` --symbols ${p.symbols.join(' ')}`;
  document.getElementById('cmdBox').innerHTML = cmd;
}

async function refreshBotStatus() {
  try {
    const data = await api('?api=bot_status');
    botRunning = data.running;
    updateStatusUI(botRunning);
    // If SSE not connected, set log from polling
    if (!sseSource && data.logs) {
      const logEl = document.getElementById('logOut');
      logEl.className = 'log-out' + (data.logs.trim()?'':' muted');
      logEl.textContent = data.logs.trim() || (botRunning?'Waiting for output…':'Bot is not running.');
      logEl.scrollTop = logEl.scrollHeight;
    }
  } catch(e) {}
}

function updateStatusUI(running) {
  const dot = document.getElementById('statusDot');
  const lbl = document.getElementById('statusLabel');
  const pip = document.getElementById('tp-control');
  const stopBtn = document.getElementById('stopBtn');
  dot.className = 'sdot '+(running?'on':'off');
  lbl.textContent = running?'Bot is RUNNING':'Bot is STOPPED';
  lbl.style.color = running?'var(--lime)':'var(--crimson)';
  if(pip) pip.style.background = running?'var(--lime)':'';
  stopBtn.disabled = !running;
}

// ══════════════════════════════════════════════════════════════
// SSE LOGS
// ══════════════════════════════════════════════════════════════
function connectSseLogs() {
  if (sseSource) return;
  const logEl = document.getElementById('logOut');
  const dot = document.getElementById('sseDot');
  const lbl = document.getElementById('sseLabel');
  dot.className = 'sse-dot conn';
  lbl.textContent = 'connecting…';

  sseSource = new EventSource('?api=bot_logs_sse');

  sseSource.onopen = () => {
    lbl.textContent = 'live';
  };

  sseSource.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      botRunning = d.running;
      updateStatusUI(d.running);
      if (d.logs && d.logs.trim()) {
        logEl.className = 'log-out';
        logEl.textContent = d.logs;
        logEl.scrollTop = logEl.scrollHeight;
      } else {
        logEl.className = 'log-out muted';
        logEl.textContent = d.running ? 'Waiting for output…' : 'Bot is not running.';
      }
    } catch(e) {}
  };

  sseSource.onerror = () => {
    dot.className = 'sse-dot';
    lbl.textContent = 'reconnecting…';
    disconnectSseLogs();
    // Retry after 5s
    setTimeout(() => {
      if (document.getElementById('tab-control').classList.contains('active')) {
        connectSseLogs();
      }
    }, 5000);
  };
}

function disconnectSseLogs() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  const dot = document.getElementById('sseDot');
  const lbl = document.getElementById('sseLabel');
  if(dot) dot.className = 'sse-dot';
  if(lbl) lbl.textContent = 'disconnected';
}

async function startBot() {
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="width:13px;height:13px;border-width:2px"></div> Starting…';
  try {
    const res = await post('?api=bot_start', buildParams());
    if (res.success) {
      setTimeout(async () => { await refreshBotStatus(); await loadSessions(); }, 2500);
    } else {
      alert('Failed to start bot.\n' + (res.out||''));
    }
  } catch(e) { alert('Error: '+e.message); }
  btn.disabled = false;
  btn.innerHTML = '🚀 Start Bot';
}

async function stopBot() {
  if (!confirm('Send Ctrl+C and kill bbot session?')) return;
  const btn = document.getElementById('stopBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spin" style="width:13px;height:13px;border-width:2px"></div> Stopping…';
  try {
    await post('?api=bot_stop', {});
    setTimeout(async () => { await refreshBotStatus(); await loadSessions(); btn.innerHTML='⏹ Stop Bot'; }, 2500);
  } catch(e) { btn.disabled=false; btn.innerHTML='⏹ Stop Bot'; }
}

// ══════════════════════════════════════════════════════════════
// SYMBOL CHECKLIST
// ══════════════════════════════════════════════════════════════
function initSymChecklist() {
  const wrap = document.getElementById('symChecklist');
  wrap.innerHTML = ALL_SYMS.map(s => `
    <label class="sym-chk-lbl">
      <input type="checkbox" id="sym_${s}" value="${s}" checked onchange="updateSymCount();updateCmd()">
      ${s}
    </label>`).join('');
  updateSymCount();
}
function getSelSyms() { return ALL_SYMS.filter(s=>document.getElementById('sym_'+s)?.checked); }
function updateSymCount() {
  const el = document.getElementById('symCount');
  if(el) el.textContent = `(${getSelSyms().length}/${ALL_SYMS.length})`;
}
function symAll(v) { ALL_SYMS.forEach(s=>{const c=document.getElementById('sym_'+s);if(c)c.checked=v}); updateSymCount(); updateCmd(); }

// ══════════════════════════════════════════════════════════════
// ML TRAINING
// ══════════════════════════════════════════════════════════════
let mlFiles = [];

async function refreshMlStatus() {
  const dot2 = document.getElementById('mlDot2');
  const txt = document.getElementById('mlStatusTxt');
  const grid = document.getElementById('mlMetaGrid');
  const thrWrap = document.getElementById('mlThrWrap');
  const pip = document.getElementById('tp-training');
  try {
    const s = await api('?api=ml_model_status');
    if (!s.trained) {
      dot2.style.background='var(--crimson)';
      txt.textContent = 'Not trained — no data/ml_filter.pkl';
      grid.style.display='none'; thrWrap.style.display='none';
      if(pip) pip.style.background='';
      return;
    }
    dot2.style.background='var(--lime)';
    txt.textContent = 'Trained — '+fmtTs(s.pkl_mtime);
    if(pip) pip.style.background='var(--lime)';
    const m = s.meta||{};
    if (m && Object.keys(m).length) {
      const teAuc = m.test?.auc??null;
      const aucCol = teAuc>0.55?'var(--lime)':teAuc>0.52?'var(--gold)':'var(--crimson)';
      grid.innerHTML = `
        <div class="ml-item"><div class="mk">Model</div><div class="mv">${m.model_kind||'—'}</div></div>
        <div class="ml-item"><div class="mk">Threshold</div><div class="mv">${(m.threshold??0).toFixed(2)}</div></div>
        <div class="ml-item"><div class="mk">Session Trades</div><div class="mv">${m.n_session_trades||0}</div></div>
        <div class="ml-item"><div class="mk">History Trades</div><div class="mv" style="color:${(m.n_history_trades||0)>0?'var(--sky)':'var(--text3)'}">${m.n_history_trades||0}</div></div>
        <div class="ml-item"><div class="mk">Train / Test</div><div class="mv">${m.n_train||0} / ${m.n_test||0}</div></div>
        <div class="ml-item"><div class="mk">Train AUC</div><div class="mv">${m.train?.auc!=null?m.train.auc.toFixed(3):'—'}</div></div>
        <div class="ml-item"><div class="mk">Test AUC</div><div class="mv" style="color:${aucCol}">${teAuc!=null?teAuc.toFixed(3):'—'}</div></div>
        <div class="ml-item"><div class="mk">Base WR</div><div class="mv">${m.test?.base_wr!=null?(m.test.base_wr*100).toFixed(1)+'%':'—'}</div></div>
      `;
      grid.style.display='';
      const thrs = m.test?.thresholds||[];
      if (thrs.length) {
        document.getElementById('mlThrTable').innerHTML =
          `<thead><tr><th>P(win) ≥</th><th>Kept</th><th>Keep %</th><th>Win Rate</th></tr></thead><tbody>`
          + thrs.map(r=>`<tr><td>${r.p.toFixed(2)}</td><td>${r.n}</td><td>${(r.keep_frac*100).toFixed(1)}%</td><td style="color:${r.wr>0.52?'var(--lime)':r.wr<0.5?'var(--crimson)':'var(--text2)'}">${r.wr!=null?(r.wr*100).toFixed(1)+'%':'—'}</td></tr>`).join('')
          + '</tbody>';
        thrWrap.style.display='';
      } else thrWrap.style.display='none';
    }
  } catch(e) { txt.textContent='Error: '+e.message; }
}

async function loadMlFiles() {
  const listEl = document.getElementById('mlFileList');
  const cnt = document.getElementById('mlFileCount');
  try {
    mlFiles = await api('?api=ml_files');
    if (!mlFiles.length) { listEl.innerHTML='<div style="padding:10px;color:var(--text3);font-size:.75rem">No trade logs found</div>'; cnt.textContent=''; return; }
    cnt.textContent = `(${mlFiles.length})`;
    listEl.innerHTML = mlFiles.map(f=>`
      <label class="file-item">
        <input type="checkbox" class="ml-file-chk" value="${f.file}" checked>
        <span class="fn">${f.file}</span>
        <span class="fm">${f.labeled} · ${fmtTs(f.mtime)}</span>
      </label>`).join('');
  } catch(e) { listEl.innerHTML=`<div style="padding:10px;color:var(--crimson);font-size:.75rem">Error: ${e.message}</div>`; }
}
function mlSelAll(v) { document.querySelectorAll('.ml-file-chk').forEach(c=>c.checked=v); }

async function trainModel() {
  const btn = document.getElementById('mlTrainBtn');
  const out = document.getElementById('mlOut');
  const include = [...document.querySelectorAll('.ml-file-chk:checked')].map(c=>c.value);
  if (!include.length) { alert('Select at least one training file.'); return; }
  btn.disabled=true; btn.innerHTML='<div class="spin" style="width:13px;height:13px;border-width:2px"></div> Training…';
  out.className='ml-out'; out.textContent='Running…\n';
  try {
    const res = await post('?api=ml_train', {
      model: document.getElementById('fMlModel').value,
      threshold: parseFloat(document.getElementById('fMlThr').value)||0.5,
      test_frac: parseFloat(document.getElementById('fMlFrac').value)||0.2,
      min_trades: parseInt(document.getElementById('fMlMin').value)||200,
      history_weight: parseFloat(document.getElementById('fMlHist').value)||0.5,
      no_history: document.getElementById('tNoHist').checked,
      include
    });
    out.textContent = `[${res.success?'SUCCESS':'FAILED'}] exit=${res.return_code} · ${res.elapsed_sec}s\n$ ${res.command}\n${'─'.repeat(60)}\n${res.output||'(no output)'}`;
    if(res.success) await refreshMlStatus();
  } catch(e) { out.textContent='Error: '+e.message; }
  btn.disabled=false; btn.innerHTML='🔬 Train Model';
}

async function fetchHistory() {
  const btn = document.getElementById('fetchHistBtn');
  const out = document.getElementById('fetchHistOut');
  btn.disabled=true; btn.innerHTML='<div class="spin" style="width:13px;height:13px;border-width:2px"></div> Fetching…';
  out.style.display=''; out.className='ml-out'; out.textContent=`Fetching ${document.getElementById('fHistHours').value}h of history…\n`;
  try {
    const res = await post('?api=fetch_history', {
      hours: parseFloat(document.getElementById('fHistHours').value)||48,
      app_id: parseInt(document.getElementById('fHistAppId').value)||1089
    });
    out.textContent = `[${res.success?'SUCCESS':'FAILED'}] ${res.elapsed_sec}s\n${res.output||''}`;
    if(res.success) await loadMlFiles();
  } catch(e) { out.textContent='Error: '+e.message; }
  btn.disabled=false; btn.innerHTML='📡 Fetch & Simulate';
}

// ══════════════════════════════════════════════════════════════
// DAEMON
// ══════════════════════════════════════════════════════════════
async function startDaemon() {
  const btn = document.getElementById('daemonStartBtn');
  btn.disabled=true; btn.innerHTML='<div class="spin" style="width:13px;height:13px;border-width:2px"></div> Starting…';
  try {
    await fetch('?api=daemon_start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval:parseInt(document.getElementById('fDaemonInterval').value),app_id:parseInt(document.getElementById('fDaemonAppId').value)})});
  } catch(e) {}
  setTimeout(refreshDaemonStatus, 1500);
}
async function stopDaemon() {
  try { await fetch('?api=daemon_stop',{method:'POST'}); } catch(e) {}
  setTimeout(refreshDaemonStatus, 1000);
}
async function refreshDaemonStatus() {
  try {
    const d = await api('?api=daemon_status');
    const dot = document.getElementById('daemonDot');
    const lbl = document.getElementById('daemonLabel');
    const sBtn = document.getElementById('daemonStartBtn');
    const xBtn = document.getElementById('daemonStopBtn');
    if(d.running){
      dot.className='sdot on'; lbl.textContent='Running'+(d.file_age_seconds!=null?' (data '+d.file_age_seconds+'s ago)':''); lbl.style.color='var(--lime)';
      sBtn.style.display='none'; xBtn.style.display='';
    } else {
      dot.className='sdot off'; lbl.textContent='Stopped'; lbl.style.color='var(--text3)';
      sBtn.style.display=''; sBtn.disabled=false; sBtn.innerHTML='▶ Start Daemon'; xBtn.style.display='none';
    }
  } catch(e) {}
}

// ══════════════════════════════════════════════════════════════
// SCANNER
// ══════════════════════════════════════════════════════════════
function startScan() {
  const hours = document.getElementById('fScanHours').value;
  const appId = document.getElementById('fScanAppId').value;
  const btn = document.getElementById('scanBtn');
  const stop = document.getElementById('scanStopBtn');
  const prog = document.getElementById('scanProgress');
  const wrap = document.getElementById('scanResultsWrap');
  const grid = document.getElementById('scanResults');
  if (scanSource){scanSource.close();scanSource=null;}
  scanResults=[]; btn.disabled=true; btn.innerHTML='<div class="spin" style="width:12px;height:12px;border-width:2px"></div> Scanning…';
  stop.style.display=''; prog.style.display='flex'; wrap.style.display=''; grid.innerHTML='';
  document.getElementById('scanProgressFill').style.width='0%';
  document.getElementById('scanProgressTxt').textContent='Connecting…';

  scanSource = new EventSource(`?api=scan_market&hours=${hours}&app_id=${appId}`);
  scanSource.onmessage = function(ev) {
    let d; try{d=JSON.parse(ev.data)}catch(e){return;}
    if(d.type==='start'){document.getElementById('scanProgressTxt').textContent=`Scanning ${d.symbols.length} symbols…`;}
    else if(d.type==='progress'){
      document.getElementById('scanProgressFill').style.width=Math.round((d.step/d.total)*100)+'%';
      document.getElementById('scanProgressTxt').textContent=d.message;
    }
    else if(d.type==='result'){scanResults.push(d);renderScanCard(d,grid);document.getElementById('scanResultCount').textContent=`(${scanResults.length})`;}
    else if(d.type==='done'){finishScan();sortScanCards();if(document.getElementById('tAutoExclude').checked) applyAllScanResults();}
    else if(d.type==='error' && d.symbol!=='__connection__'){
      const e=document.createElement('div');e.className='scan-card danger';
      e.innerHTML=`<div class="scan-head"><span class="scan-sym">${d.symbol||'Error'}</span><span class="sig-badge sig-no">ERROR</span></div><div style="font-size:.74rem;color:var(--crimson)">${d.message}</div>`;
      grid.appendChild(e);
    }
  };
  scanSource.onerror=()=>{finishScan();if(scanResults.length)sortScanCards();};
}
function stopScan(){if(scanSource){scanSource.close();scanSource=null;}finishScan();}
function finishScan(){
  const btn=document.getElementById('scanBtn');
  btn.disabled=false;btn.innerHTML='🔍 Manual Scan';
  document.getElementById('scanStopBtn').style.display='none';
  document.getElementById('scanProgress').style.display='none';
  document.getElementById('scanTimestamp').textContent='Scanned '+new Date().toLocaleTimeString();
  if(scanSource){scanSource.close();scanSource=null;}
}
function sortScanCards(){
  scanResults.sort((a,b)=>(b.recommendation?.tradability||0)-(a.recommendation?.tradability||0));
  const grid=document.getElementById('scanResults'); grid.innerHTML='';
  scanResults.forEach(d=>renderScanCard(d,grid));
}
function renderScanCard(d,container) {
  if(d.status==='NO_DATA') return;
  const r=d.recommendation||{}, v=d.volatility||{}, dg=d.digits||{}, p=d.patterns||{};
  const sig=r.entry_signal||'WAIT', trad=r.tradability||0;
  let cc='scan-card',bc='sig-badge sig-wait';
  if(sig==='STRONG_ENTRY'){cc+=' strong';bc='sig-badge sig-strong';}
  else if(sig==='GOOD_ENTRY'){cc+=' good';bc='sig-badge sig-good';}
  else if(sig==='DO_NOT_ENTER'){cc+=' danger';bc='sig-badge sig-no';}
  else cc+=' wait';
  const tradC=trad>=70?'var(--lime)':trad>=45?'var(--gold)':'var(--crimson)';
  const rc={MEAN_REVERTING:'var(--lime)',TRENDING:'var(--sky)',CHOPPY:'var(--crimson)',UNKNOWN:'var(--text3)'};
  const vc={LOW:'var(--lime)',MODERATE:'var(--sky)',HIGH:'var(--gold)',EXTREME:'var(--crimson)',UNKNOWN:'var(--text3)'};
  const algoN={alphabloom:'AlphaBloom',pulse:'Pulse',ensemble:'Ensemble',novaburst:'NovaBurst',adaptive:'Adaptive'};
  const tsN={even_odd:'Even/Odd',rise_fall_roll:'Rise/Fall Roll',rise_fall_zigzag:'Rise/Fall Zigzag',higher_lower_roll:'Higher/Lower Roll',higher_lower_zigzag:'Higher/Lower Zigzag',over_under_roll:'Over/Under Roll',touch_notouch_zigzag:'Touch/NoTouch Zigzag'};
  const card=document.createElement('div'); card.className=cc;
  card.innerHTML=`
    <div class="scan-head"><span class="scan-sym">${d.symbol}</span><span class="${bc}">${sig.replace(/_/g,' ')}</span></div>
    <div class="scan-metrics">
      <div class="scan-m"><div class="sm-k">Regime</div><div class="sm-v" style="color:${rc[d.regime]||'var(--text)'}">${d.regime||'—'}</div></div>
      <div class="scan-m"><div class="sm-k">Volatility</div><div class="sm-v" style="color:${vc[v.level]||'var(--text)'}">${v.level||'—'}</div></div>
      <div class="scan-m"><div class="sm-k">ATR %ile</div><div class="sm-v">${v.atr_percentile!=null?v.atr_percentile.toFixed(0)+'%':'—'}</div></div>
      <div class="scan-m"><div class="sm-k">P(even)</div><div class="sm-v">${dg.p_even!=null?(dg.p_even*100).toFixed(1)+'%':'—'}</div></div>
      <div class="scan-m"><div class="sm-k">Bias</div><div class="sm-v" style="color:${dg.is_biased?'var(--lime)':'var(--text3)'}">${dg.is_biased?'YES':'no'}</div></div>
      <div class="scan-m"><div class="sm-k">Momentum</div><div class="sm-v">${v.momentum!=null?(v.momentum>=0?'+':'')+v.momentum.toFixed(4):'—'}</div></div>
    </div>
    <div class="scan-pats">
      <span class="scan-pat ${(p.pulse?.score||0)>=0.4?'on':''}">Pulse ${p.pulse?.score!=null?p.pulse.score.toFixed(2):'—'}</span>
      <span class="scan-pat ${(p.rollcake?.score||0)>=0.3?'on':''}">Roll ${p.rollcake?.score!=null?p.rollcake.score.toFixed(2):'—'}</span>
      <span class="scan-pat ${(p.zigzag?.score||0)>=0.3?'on':''}">Zigzag ${p.zigzag?.score!=null?p.zigzag.score.toFixed(2):'—'}</span>
    </div>
    <div class="scan-rec">
      <div class="scan-rec-txt"><strong>${algoN[r.algorithm]||r.algorithm||'—'}</strong> + ${tsN[r.trade_strategy]||r.trade_strategy||'—'}</div>
      ${sig!=='DO_NOT_ENTER'?`<button class="scan-use" onclick="applyScanRec('${r.algorithm}','${r.trade_strategy}')">Use</button>`:''}
    </div>
    <div class="trad-bar"><div class="trad-fill" style="width:${trad}%;background:${tradC}"></div></div>
    <div class="scan-footer">Tradability ${trad}/100</div>
  `;
  container.appendChild(card);
}
function applyScanRec(algo, ts) {
  const ae=document.getElementById('fStrategy'); if(ae){ae.value=algo;onStrategyChange();}
  const te=document.getElementById('fTradeStrategy'); if(te) te.value=ts;
  updateCmd(); switchTab('control');
}
function applyAllScanResults() {
  if(!scanResults.length) return;
  const autoEx=document.getElementById('tAutoExclude').checked;
  const autoSt=document.getElementById('tAutoStrategy').checked;
  scanResults.forEach(d=>{
    const sig=d.recommendation?.entry_signal||'WAIT';
    const chk=document.getElementById('sym_'+d.symbol);
    if(chk){ if(autoEx&&sig==='DO_NOT_ENTER') chk.checked=false; else chk.checked=true; }
  });
  updateSymCount();
  if(autoSt && scanResults.length){
    const best=scanResults.find(d=>(d.recommendation?.entry_signal||'')!=='DO_NOT_ENTER');
    if(best?.recommendation){
      const ae=document.getElementById('fStrategy'); if(ae){ae.value=best.recommendation.algorithm;onStrategyChange();}
      const te=document.getElementById('fTradeStrategy'); if(te) te.value=best.recommendation.trade_strategy;
    }
  }
  updateCmd();
}
async function refreshFromDaemon() {
  try {
    const data=await api('?api=daemon_scan');
    if(data.error){alert(data.error);return;}
    const results=data.results||[];
    scanResults=[]; const grid=document.getElementById('scanResults');
    const wrap=document.getElementById('scanResultsWrap');
    grid.innerHTML=''; wrap.style.display='';
    results.forEach(d=>{
      d.recommendation=recommendForSym(d); d.type='result'; d.status=d.status||'OK';
      scanResults.push(d); renderScanCard(d,grid);
    });
    document.getElementById('scanResultCount').textContent=`(${scanResults.length})`;
    document.getElementById('scanTimestamp').textContent='Daemon: '+(data.updated_at||new Date().toLocaleTimeString());
    if(document.getElementById('tAutoExclude').checked||autoScanRunning) applyAllScanResults();
  } catch(e){console.error(e);}
}
function toggleAutoScan(){autoScanRunning?stopAutoScan():startAutoScan();}
function startAutoScan(){
  const iv=parseFloat(document.getElementById('fScanInterval').value)||0;
  if(iv<=0){alert('Set Auto-Refresh interval first.');return;}
  autoScanRunning=true;
  const btn=document.getElementById('autoScanBtn');
  btn.innerHTML='⏹ Stop Auto-Refresh'; btn.className='btn btn-red';
  refreshFromDaemon();
  autoScanTimer=setInterval(refreshFromDaemon, iv*60*1000);
}
function stopAutoScan(){
  autoScanRunning=false;
  if(autoScanTimer){clearInterval(autoScanTimer);autoScanTimer=null;}
  const btn=document.getElementById('autoScanBtn');
  btn.innerHTML='↻ Start Auto-Refresh'; btn.className='btn btn-ghost';
}
function getAllowedAlgos(){return[...document.querySelectorAll('.algoFilterChk:checked')].map(c=>c.value);}
function getAllowedTS(){return[...document.querySelectorAll('.tsFilterChk:checked')].map(c=>c.value);}
function recommendForSym(d){
  const aa=getAllowedAlgos(), at=getAllowedTS();
  const v=d.volatility||{}, dg=d.digits||{}, p=d.patterns||{};
  const regime=d.regime||'UNKNOWN', vol=v.level||'UNKNOWN', trad=d.tradability||0;
  const bm=dg.bias_magnitude||0;
  const pA=(a)=>aa.includes(a)?a:(aa[0]||'adaptive');
  const pT=(t)=>at.includes(t)?t:(at[0]||'even_odd');
  const mk=(cs)=>{for(const c of cs)if(aa.includes(c.a)&&at.includes(c.t))return{algorithm:c.a,trade_strategy:c.t,entry_signal:c.s,tradability:trad};return{algorithm:pA('adaptive'),trade_strategy:pT('even_odd'),entry_signal:'WAIT',tradability:trad};};
  if(vol==='EXTREME') return{algorithm:pA('adaptive'),trade_strategy:pT('even_odd'),entry_signal:'DO_NOT_ENTER',tradability:trad};
  if(vol==='HIGH'){
    if(dg.is_biased&&bm>=0.10&&(p.pulse?.score||0)>=0.65) return mk([{a:'pulse',t:'even_odd',s:'GOOD_ENTRY'}]);
    if(dg.is_biased&&bm>=0.08) return mk([{a:'alphabloom',t:'even_odd',s:'WAIT'}]);
    return{algorithm:pA('adaptive'),trade_strategy:pT('even_odd'),entry_signal:'DO_NOT_ENTER',tradability:trad};
  }
  const ps=p.pulse?.score||0, rs=p.rollcake?.score||0, zs=p.zigzag?.score||0, biased=dg.is_biased;
  const cands=[];
  if(regime==='MEAN_REVERTING'&&(vol==='LOW'||vol==='MODERATE')){
    if(biased&&bm>=0.06&&ps>=0.6) cands.push({a:'pulse',t:'even_odd',s:'STRONG_ENTRY'});
    if(biased&&bm>=0.06&&ps>=0.4) cands.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    if(biased&&bm>=0.06) cands.push({a:'alphabloom',t:'even_odd',s:'GOOD_ENTRY'});
    if(rs>=0.70&&vol==='LOW') cands.push({a:'pulse',t:'rise_fall_roll',s:'GOOD_ENTRY'});
    cands.push({a:'ensemble',t:'even_odd',s:'WAIT'});
  } else if(regime==='TRENDING'){
    if(biased&&ps>=0.55) cands.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    if(biased&&bm>=0.08) cands.push({a:'alphabloom',t:'even_odd',s:'GOOD_ENTRY'});
    if(vol==='LOW'&&rs>=0.70) cands.push({a:'pulse',t:'rise_fall_roll',s:'GOOD_ENTRY'});
    if(vol==='LOW'&&zs>=0.65) cands.push({a:'novaburst',t:'rise_fall_zigzag',s:'GOOD_ENTRY'});
    cands.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  } else {
    if(biased&&bm>=0.08&&ps>=0.55) cands.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
    cands.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  }
  if(biased&&ps>=0.55) cands.push({a:'pulse',t:'even_odd',s:'GOOD_ENTRY'});
  cands.push({a:'adaptive',t:'even_odd',s:'WAIT'});
  return mk(cands);
}

// ══════════════════════════════════════════════════════════════
// UTILS
// ══════════════════════════════════════════════════════════════
function fmtTs(ts) {
  if(!ts) return '—';
  const d=new Date(ts*1000);
  return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'})+' '+d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
}
function computeDuration(start,end) {
  if(!start) return '—';
  const diff=Math.floor(((end?end:Date.now()/1000)-start));
  if(diff<60) return diff+'s';
  if(diff<3600) return Math.floor(diff/60)+'m '+(diff%60)+'s';
  return Math.floor(diff/3600)+'h '+Math.floor((diff%3600)/60)+'m';
}
function contractBadge(ct) {
  if(!ct) return '<span class="badge">—</span>';
  if(ct.includes('ODD')) return '<span class="badge bo">ODD</span>';
  if(ct.includes('EVEN')) return '<span class="badge be">EVEN</span>';
  if(ct==='CALL') return '<span class="badge bc">CALL ↑</span>';
  if(ct==='PUT') return '<span class="badge bp">PUT ↓</span>';
  if(ct==='DIGITOVER') return '<span class="badge bov">OVER</span>';
  if(ct==='DIGITUNDER') return '<span class="badge bun">UNDER</span>';
  if(ct==='ONETOUCH') return '<span class="badge bto">TOUCH</span>';
  if(ct==='NOTOUCH') return '<span class="badge bnt">NO TOUCH</span>';
  return `<span class="badge">${ct}</span>`;
}

// ══════════════════════════════════════════════════════════════
// BOOT
// ══════════════════════════════════════════════════════════════
init();
refreshDaemonStatus();
</script>
</body>
</html>