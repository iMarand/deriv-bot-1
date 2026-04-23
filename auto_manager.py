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
    
    if vol_level == 'EXTREME': return None
    if vol_level == 'HIGH':
        if dg.get('is_biased') and bias_mag >= 0.10 and p.get('pulse', {}).get('score', 0) >= 0.65:
            return match_filter([{'a':'pulse','t':'even_odd','s':'GOOD_ENTRY'}], allowed_algos, allowed_ts)
        return None
        
    candidates = []
    ps = p.get('pulse', {}).get('score', 0)
    rs = p.get('rollcake', {}).get('score', 0)
    regime = d.get('regime', 'UNKNOWN')
    
    if regime == 'MEAN_REVERTING' and vol_level in ['LOW', 'MODERATE']:
        if dg.get('is_biased') and bias_mag >= 0.06 and ps >= 0.6: candidates.append({'a':'pulse','t':'even_odd','s':'STRONG_ENTRY'})
        if dg.get('is_biased') and bias_mag >= 0.06 and ps >= 0.4: candidates.append({'a':'pulse','t':'even_odd','s':'GOOD_ENTRY'})
        if dg.get('is_biased') and bias_mag >= 0.06: candidates.append({'a':'alphabloom','t':'even_odd','s':'GOOD_ENTRY'})
        if rs >= 0.70 and vol_level == 'LOW': candidates.append({'a':'pulse','t':'rise_fall_roll','s':'GOOD_ENTRY'})
        candidates.append({'a':'ensemble','t':'even_odd','s':'WAIT'})
        
    if regime == 'TRENDING':
        if dg.get('is_biased') and ps >= 0.55: candidates.append({'a':'pulse','t':'even_odd','s':'GOOD_ENTRY'})
        if dg.get('is_biased') and bias_mag >= 0.08: candidates.append({'a':'alphabloom','t':'even_odd','s':'GOOD_ENTRY'})
        if vol_level == 'LOW' and rs >= 0.70: candidates.append({'a':'pulse','t':'rise_fall_roll','s':'GOOD_ENTRY'})
        candidates.append({'a':'adaptive','t':'even_odd','s':'WAIT'})
        
    if regime == 'CHOPPY':
        if dg.get('is_biased') and bias_mag >= 0.08 and ps >= 0.55: candidates.append({'a':'pulse','t':'even_odd','s':'GOOD_ENTRY'})
        candidates.append({'a':'adaptive','t':'even_odd','s':'WAIT'})
        
    if dg.get('is_biased') and ps >= 0.55: candidates.append({'a':'pulse','t':'even_odd','s':'GOOD_ENTRY'})
    candidates.append({'a':'adaptive','t':'even_odd','s':'WAIT'})
    
    return match_filter(candidates, allowed_algos, allowed_ts)

def restart_bot(base_cmd, algo, ts, symbols):
    logger.info(f"Restarting bot with {algo} / {ts} on {len(symbols)} symbols")
    
    cmd = base_cmd
    import re
    cmd = re.sub(r'--strategy \S+', '', cmd)
    cmd = re.sub(r'--trade-strategy \S+', '', cmd)
    cmd = re.sub(r'--symbols.*', '', cmd) # Remove trailing symbols if any
    
    cmd += f" --strategy {algo} --trade-strategy {ts}"
    cmd += " --symbols " + " ".join(symbols)
    
    launcher_path = Path(__file__).parent / ".launcher.sh"
    script = f"#!/bin/bash\ncd {Path(__file__).parent.absolute()}\necho 'Auto-Manager Restart: {cmd}'\n{cmd}\nEXIT=$?\necho 'Exited ($EXIT)'\nread\n"
    
    with open(launcher_path, "w") as f:
        f.write(script)
    os.chmod(launcher_path, 0o755)
    
    # Kill existing
    subprocess.run(f"tmux send-keys -t ={TMUX_NAME} C-c 2>&1", shell=True, capture_output=True)
    time.sleep(1)
    subprocess.run(f"tmux kill-session -t ={TMUX_NAME} 2>&1", shell=True, capture_output=True)
    subprocess.run("pkill -9 -f \" bot\\.py\" 2>&1", shell=True, capture_output=True)
    time.sleep(1)
    
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
                restart_bot(base_cmd, best_algo, best_ts, best_symbols)
                
                current_algo = best_algo
                current_ts = best_ts
                current_symbols = best_symbols
                
                write_state(f"Running {best_algo} ({best_ts}) on {len(best_symbols)} symbols", current_algo, current_ts, current_symbols)
            else:
                write_state(f"Monitoring stable. {best_algo} on {len(best_symbols)} symbols.", current_algo, current_ts, current_symbols)
                
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            write_state(f"Error: {e}", current_algo, current_ts, current_symbols)
            
        time.sleep(15)

if __name__ == "__main__":
    main()
