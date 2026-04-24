#!/usr/bin/env python3
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "autopilot_config.json"
STATE_FILE = DATA_DIR / "autopilot_state.json"
SCAN_FILE = DATA_DIR / "market_scan.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA_DIR / "autopilot.log", mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("autopilot")

def load_json(path):
    try:
        if path.exists():
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Error reading {path.name}: {e}")
    return None

def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def get_best_strategy_from_scan(allowed_algos):
    scan_data = load_json(SCAN_FILE)
    if not scan_data or "results" not in scan_data:
        return None, None
        
    best_score = -1
    best_algo = allowed_algos[0] if allowed_algos else "adaptive"
    best_ts = "even_odd"
    
    for d in scan_data["results"]:
        dg = d.get('digits', {})
        p = d.get('patterns', {})
        vol_level = d.get('volatility', {}).get('level', 'UNKNOWN')
        
        if vol_level == 'EXTREME':
            continue
            
        ps = p.get('pulse', {}).get('score', 0)
        rs = p.get('rollcake', {}).get('score', 0)
        
        # Simple heuristic: Pulse score + rollcake score + biased
        score = ps + rs
        if dg.get('is_biased'):
            score += 0.2
            
        if score > best_score:
            best_score = score
            # Pick algo based on conditions
            if "aegis" in allowed_algos and ps >= 0.55 and dg.get('is_biased'):
                best_algo = "aegis"
                best_ts = "even_odd"
            elif "novaburst" in allowed_algos and ps >= 0.6:
                best_algo = "novaburst"
                best_ts = "even_odd"
            elif "pulse" in allowed_algos:
                best_algo = "pulse"
                best_ts = "even_odd"
            else:
                best_algo = allowed_algos[0] if allowed_algos else "adaptive"
                
    return best_algo, best_ts

def run_benchmark(cfg):
    # Overwrite benchmark config
    bench_cfg = {
        "token": cfg.get("token", ""),
        "account_mode": "demo", # Always demo
        "algos": cfg.get("allowed_algos", ["pulse", "novaburst", "adaptive", "aegis"]),
        "base_stake": 1.0,
        "martingale": 2.2,
        "max_stake": 50.0,
        "profit_target": 100, # Unlikely to hit in 5 mins
        "loss_limit": -100,
        "trade_strategy": "even_odd"
    }
    write_json(DATA_DIR / "benchmark_config.json", bench_cfg)
    
    log.info("Starting background benchmark to determine best strategy...")
    subprocess.run(["tmux", "new-session", "-d", "-s", "bbot-benchmark", "python3 benchmark.py"], shell=False)
    
    wait_mins = cfg.get("benchmark_duration_minutes", 5)
    log.info(f"Waiting {wait_mins} minutes for benchmark to collect data...")
    time.sleep(wait_mins * 60)
    
    log.info("Stopping benchmark...")
    subprocess.run(["tmux", "kill-session", "-t", "=bbot-benchmark"], capture_output=True)
    subprocess.run(["pkill", "-f", "bot.py.*bench-"], capture_output=True) # Ensure bots are dead
    
    # Read benchmark state
    bench_state = load_json(DATA_DIR / "benchmark_state.json")
    if not bench_state or "runners" not in bench_state:
        log.warning("Could not read benchmark results. Falling back to default.")
        return cfg.get("allowed_algos", ["adaptive"])[0], "even_odd"
        
    best_algo = None
    best_net = -9999
    best_winrate = -1
    
    for algo, stats in bench_state["runners"].items():
        net = stats.get("net_pnl", 0)
        wr = stats.get("win_rate", 0)
        trades = stats.get("trades", 0)
        if trades > 0 and net > best_net:
            best_net = net
            best_winrate = wr
            best_algo = algo
            
    if best_algo:
        log.info(f"Benchmark winner: {best_algo} (Net: ${best_net:.2f}, WR: {best_winrate:.1%})")
        return best_algo, "even_odd"
    else:
        return cfg.get("allowed_algos", ["adaptive"])[0], "even_odd"

def main():
    log.info("=== Systematic Autopilot Started ===")
    cumulative_profit = 0.0
    sprint_count = 0
    
    while True:
        cfg = load_json(CONFIG_FILE)
        if not cfg:
            log.info("Waiting for autopilot_config.json...")
            time.sleep(5)
            continue
            
        max_daily = cfg.get("max_daily_profit", 100.0)
        if cumulative_profit >= max_daily:
            log.info(f"🎉 GOAL REACHED! Cumulative profit: ${cumulative_profit:.2f} >= ${max_daily:.2f}")
            break
            
        log.info(f"--- Sprint {sprint_count+1} | Current Session PnL: ${cumulative_profit:.2f} / ${max_daily:.2f} ---")
        
        # 1. Strategy Selection
        algo = None
        ts = None
        if cfg.get("use_benchmark", False):
            algo, ts = run_benchmark(cfg)
        else:
            algo, ts = get_best_strategy_from_scan(cfg.get("allowed_algos", ["adaptive", "aegis"]))
            
        if not algo:
            algo = "adaptive"
            ts = "even_odd"
            
        # 2. Dynamic Parameters
        stake_min, stake_max = cfg.get("stake_range", [0.5, 2.0])
        tp_min, tp_max = cfg.get("sprint_tp_range", [5.0, 15.0])
        sl_min, sl_max = cfg.get("sprint_sl_range", [-50.0, -20.0])
        
        stake = round(random.uniform(stake_min, stake_max), 2)
        tp = round(random.uniform(tp_min, tp_max), 2)
        # Random between two negative numbers (e.g. -50 to -20)
        sl = round(random.uniform(min(sl_min, sl_max), max(sl_min, sl_max)), 2)
        
        log.info(f"🎯 Selected: {algo} ({ts}) | Stake: ${stake:.2f} | Target: +${tp:.2f} | Stop: ${sl:.2f}")
        
        # Write state to display in UI
        write_json(STATE_FILE, {
            "cumulative_profit": cumulative_profit,
            "max_daily_profit": max_daily,
            "sprint_count": sprint_count,
            "current_algo": algo,
            "current_stake": stake,
            "current_tp": tp,
            "current_sl": sl,
            "status": "RUNNING SPRINT",
            "updated_at": time.time()
        })
        
        # 3. Execution
        app_json = DATA_DIR / "autopilot_sprint.json"
        
        # Build command
        cmd = [
            "python3", "bot.py",
            "--token", cfg.get("token", ""),
            "--account-mode", cfg.get("account_mode", "demo"),
            "--strategy", algo,
            "--trade-strategy", ts,
            "--base-stake", str(stake),
            "--profit-target", str(tp),
            "--loss-limit", str(sl),
            "--martingale", str(cfg.get("martingale", 2.2)),
            "--max-stake", str(cfg.get("max_stake", 5000.0)),
            "--app-json", str(app_json),
            "--disable-hot-reload" # Let autopilot manage it
        ]
        
        # Handle all symbols logic like auto_manager does
        symbols = cfg.get("symbols", ["R_10","R_25","R_50","R_75","R_100","1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V"])
        cmd += ["--symbols"] + symbols
        
        log.info(f"🚀 Launching sprint bot...")
        
        # Start bot and wait for it to finish
        bot_log = DATA_DIR / "autopilot_bot.log"
        with open(bot_log, "w") as f:
            f.write(f"=== SPRINT {sprint_count+1} ===\n")
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
            
        log.info(f"Bot exited with code {proc.returncode}")
        
        # 4. Result Resolution
        sprint_net = 0.0
        session_data = load_json(app_json)
        if session_data and "summary" in session_data:
            sprint_net = session_data["summary"].get("net_pnl", 0.0)
            trades = session_data["summary"].get("trade_count", 0)
            wins = session_data["summary"].get("wins", 0)
            log.info(f"🏁 Sprint Finished: {trades} trades, {wins} wins. Net PnL: ${sprint_net:.2f}")
        else:
            log.warning("Could not read sprint result JSON. Assuming 0.")
            
        cumulative_profit += sprint_net
        sprint_count += 1
        
        # Write state again
        write_json(STATE_FILE, {
            "cumulative_profit": cumulative_profit,
            "max_daily_profit": max_daily,
            "sprint_count": sprint_count,
            "current_algo": "COOLDOWN",
            "last_sprint_pnl": sprint_net,
            "status": "COOLDOWN",
            "updated_at": time.time()
        })
        
        if cumulative_profit >= max_daily:
            continue # Let the loop catch it at the top
            
        # 5. Cooldown
        if sprint_net > 0:
            cd = cfg.get("cooldown_win_minutes", 2)
            log.info(f"✅ Sprint was profitable. Cooling down for {cd} minutes.")
        else:
            cd = cfg.get("cooldown_loss_minutes", 5)
            log.info(f"❌ Sprint was a loss. Cooling down for {cd} minutes to let market recover.")
            
        time.sleep(cd * 60)

if __name__ == "__main__":
    main()
