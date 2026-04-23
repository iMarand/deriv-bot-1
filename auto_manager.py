"""
Auto-Trader Manager

Runs in the background, reading market_scan.json and manager_config.json.
Determines optimal symbols based on the user's allowed algorithms/strategies,
and if they differ from the currently running bot, restarts the bot via tmux.
"""

import json
import time
import os
import subprocess
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("auto_manager")

DATA_DIR = Path(__file__).parent / "data"
SCAN_FILE = DATA_DIR / "market_scan.json"
CONFIG_FILE = DATA_DIR / "manager_config.json"
STATE_FILE = DATA_DIR / "manager_state.json"
TMUX_NAME = "bbot"

def load_json(path):
    try:
        if path.exists():
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error reading {path.name}: {e}")
    return None

def write_state(status_msg, current_algo, current_ts, current_symbols):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "status_msg": status_msg,
                "algorithm": current_algo,
                "trade_strategy": current_ts,
                "symbols": current_symbols,
                "updated_at": time.time()
            }, f)
    except Exception as e:
        logger.error(f"Error writing state: {e}")

def match_filter(candidates, allowed_algos, allowed_ts):
    for c in candidates:
        if c['a'] in allowed_algos and c['t'] in allowed_ts:
            return c
    return None

def recommend_for_symbol(d, allowed_algos, allowed_ts):
    v = d.get('volatility', {})
    dg = d.get('digits', {})
    p = d.get('patterns', {})
    vol_level = v.get('level', 'UNKNOWN')
    bias_mag = dg.get('bias_magnitude', 0)
    
    ps = p.get('pulse', {}).get('score', 0)
    rs = p.get('rollcake', {}).get('score', 0)
    zs = p.get('zigzag', {}).get('score', 0)
    
    candidates = []
    
    if vol_level == 'EXTREME':
        return match_filter([{'a': a, 't': t, 's': 'DO_NOT_ENTER'} for a in allowed_algos for t in allowed_ts], allowed_algos, allowed_ts)
        
    for a in allowed_algos:
        for t in allowed_ts:
            s = 'WAIT'
            
            # Base algorithm conditions
            algo_ready = False
            if a in ['pulse', 'ensemble', 'novaburst', 'adaptive']:
                if dg.get('is_biased') and ps >= 0.55:
                    algo_ready = True
            elif a == 'alphabloom':
                if dg.get('is_biased') and bias_mag >= 0.08:
                    algo_ready = True
                    
            # Trade strategy conditions
            ts_ready = False
            if t == 'even_odd':
                ts_ready = True # even_odd only requires algo_ready
            elif 'roll' in t:
                if rs >= 0.70 and vol_level in ['LOW', 'MODERATE']:
                    ts_ready = True
            elif 'zigzag' in t:
                if zs >= 0.70 and vol_level in ['LOW', 'MODERATE']:
                    ts_ready = True
                    
            if algo_ready and ts_ready:
                s = 'GOOD_ENTRY'
                if a == 'pulse' and ps >= 0.65 and vol_level != 'HIGH':
                    s = 'STRONG_ENTRY'
                    
            candidates.append({'a': a, 't': t, 's': s})
            
    # Sort candidates so STRONG_ENTRY > GOOD_ENTRY > WAIT
    rank = {'STRONG_ENTRY': 3, 'GOOD_ENTRY': 2, 'WAIT': 1, 'DO_NOT_ENTER': 0}
    candidates.sort(key=lambda x: rank[x['s']], reverse=True)
    
    return match_filter(candidates, allowed_algos, allowed_ts)

def ensure_bot_running(base_cmd, algo, ts):
    # Check if running
    res = subprocess.run(f"tmux has-session -t ={TMUX_NAME} 2>/dev/null", shell=True)
    if res.returncode == 0:
        return # Already running, hot-reload will handle it via manager_state.json

    logger.info(f"Bot not running. Starting bot...")
    
    cmd = base_cmd
    import re
    cmd = re.sub(r'--strategy \S+', '', cmd)
    cmd = re.sub(r'--trade-strategy \S+', '', cmd)
    cmd = re.sub(r'--symbols.*', '', cmd) # Remove trailing symbols if any
    
    # Start with ALL symbols so bot.py warms them all up. 
    # The hot-reload mechanism will restrict trading to only the active ones.
    all_symbols = ["R_10","R_25","R_50","R_75","R_100","1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V"]
    
    cmd += f" --strategy {algo} --trade-strategy {ts}"
    cmd += " --symbols " + " ".join(all_symbols)
    
    launcher_path = Path(__file__).parent / ".launcher.sh"
    script = f"#!/bin/bash\ncd {Path(__file__).parent.absolute()}\necho 'Auto-Manager Started Bot: {cmd}'\n{cmd}\nEXIT=$?\necho 'Exited ($EXIT)'\nread\n"
    
    with open(launcher_path, "w") as f:
        f.write(script)
    os.chmod(launcher_path, 0o755)
    
    # Start new
    subprocess.run(f"tmux new-session -d -s {TMUX_NAME} bash {launcher_path.absolute()}", shell=True)

def main():
    logger.info("Auto-Manager started. Waiting for configuration...")
    
    current_algo = None
    current_ts = None
    current_symbols = []
    
    while True:
        try:
            config = load_json(CONFIG_FILE)
            if not config:
                write_state("Waiting for configuration...", current_algo, current_ts, current_symbols)
                time.sleep(5)
                continue
                
            allowed_algos = config.get("algorithms", ["adaptive"])
            allowed_ts = config.get("trade_strategies", ["even_odd"])
            base_cmd = config.get("base_cmd", "")
            
            scan_data = load_json(SCAN_FILE)
            if not scan_data or "results" not in scan_data:
                write_state("Waiting for daemon scan data...", current_algo, current_ts, current_symbols)
                time.sleep(5)
                continue
                
            # Evaluate all symbols
            recommendations = []
            for d in scan_data["results"]:
                rec = recommend_for_symbol(d, allowed_algos, allowed_ts)
                if rec and rec['s'] in ['STRONG_ENTRY', 'GOOD_ENTRY']:
                    recommendations.append({
                        'symbol': d['symbol'],
                        'algo': rec['a'],
                        'ts': rec['t'],
                        'score': d.get('patterns',{}).get('pulse',{}).get('score',0) + d.get('patterns',{}).get('rollcake',{}).get('score',0)
                    })
            
            if not recommendations:
                best_algo = allowed_algos[0] if allowed_algos else 'adaptive'
                best_ts = allowed_ts[0] if allowed_ts else 'even_odd'
                best_symbols = [d['symbol'] for d in scan_data['results']][:3]
            else:
                groups = {}
                for r in recommendations:
                    key = f"{r['algo']}|{r['ts']}"
                    if key not in groups:
                        groups[key] = []
                    groups[key].append(r)
                
                best_group_key = max(groups.keys(), key=lambda k: (len(groups[k]), sum(x['score'] for x in groups[k])))
                best_algo, best_ts = best_group_key.split('|')
                best_symbols = [x['symbol'] for x in groups[best_group_key]]
            
            if best_algo != current_algo or best_ts != current_ts or set(best_symbols) != set(current_symbols):
                logger.info(f"Configuration change detected! New config: {best_algo} / {best_ts} on {len(best_symbols)} symbols")
                
                current_algo = best_algo
                current_ts = best_ts
                current_symbols = best_symbols
                
                # Write state FIRST so that when ensure_bot_running launches the bot, 
                # the bot immediately reads the updated state file to filter symbols.
                write_state(f"Running {best_algo} ({best_ts}) on {len(best_symbols)} symbols", current_algo, current_ts, current_symbols)
                ensure_bot_running(base_cmd, best_algo, best_ts)
            else:
                write_state(f"Monitoring stable. {best_algo} on {len(best_symbols)} symbols.", current_algo, current_ts, current_symbols)
                # Ensure the bot didn't crash in the background
                ensure_bot_running(base_cmd, best_algo, best_ts)
                
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            write_state(f"Error: {e}", current_algo, current_ts, current_symbols)
            
        time.sleep(15)

if __name__ == "__main__":
    main()
