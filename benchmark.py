#!/usr/bin/env python3
"""
Benchmark Orchestrator — runs multiple algorithms simultaneously and compares results.

Config read from : data/benchmark_config.json  (written by analysis.php)
State written to : data/benchmark_state.json    (polled by analysis.php)

Each algorithm gets its own tmux session (bbot-bench-<algo>) and its own
session JSON file.  The orchestrator monitors every 5 s and shuts down any
bot whose profit_target / loss_limit / capital has been reached.
When all runners are done it writes a final comparison and exits.
"""

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR    = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "benchmark_config.json"
STATE_FILE  = DATA_DIR / "benchmark_state.json"
POLL_SEC    = 5


# ── helpers ──────────────────────────────────────────────────────────────────

def tmux_running(name: str) -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                       capture_output=True)
    return r.returncode == 0


def kill_tmux(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", f"={name}"],
                   capture_output=True)


def read_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def compute_streaks(trades: list) -> tuple:
    max_win = max_loss = cur_win = cur_loss = 0
    for t in trades:
        if t.get("result") == "win":
            cur_win += 1; cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            max_loss = max(max_loss, cur_loss)
    return max_win, max_loss


def snapshot_from_file(path: Path, profit_target: float, loss_limit: float) -> dict:
    data = read_json(path) or {}
    summary  = data.get("summary", {})
    session  = data.get("session", {})
    trades   = data.get("trades", [])
    mw, ml   = compute_streaks(trades)
    net_pnl  = summary.get("net_pnl", 0.0)
    duration = 0
    started  = session.get("started_at")
    updated  = session.get("updated_at")
    if started and updated:
        duration = int(updated - started)
    return {
        "trades":          summary.get("trade_count", 0),
        "wins":            summary.get("wins", 0),
        "losses":          summary.get("losses", 0),
        "net_pnl":         round(net_pnl, 4),
        "win_rate":        round(summary.get("win_rate", 0.0), 4),
        "initial_equity":  session.get("initial_equity"),
        "current_equity":  session.get("current_equity"),
        "max_win_streak":  mw,
        "max_loss_streak": ml,
        "duration_seconds": duration,
    }


# ── launcher ─────────────────────────────────────────────────────────────────

def build_bot_command(cfg: dict, algo: str, sess_file: Path) -> str:
    token        = cfg["token"]
    mode         = cfg.get("account_mode", "demo")
    app_id       = cfg.get("app_id", 1089)
    base_stake   = cfg.get("base_stake", 1.0)
    martingale   = cfg.get("martingale", 2.0)
    max_stake    = cfg.get("max_stake", 50.0)
    max_losses   = cfg.get("max_losses", 4)
    loss_limit   = cfg.get("loss_limit", cfg.get("capital_per_algo", 2500))
    profit_tgt   = cfg.get("profit_target", 500)
    threshold    = cfg.get("score_threshold", 0.60)
    trade_strat  = cfg.get("trade_strategy", "even_odd")
    symbols      = cfg.get("symbols", [])
    sym_str      = " ".join(symbols) if symbols else ""

    cmd = (
        f"python3 bot.py"
        f" --token {token}"
        f" --account-mode {mode}"
        f" --app-id {app_id}"
        f" --base-stake {base_stake}"
        f" --martingale {martingale}"
        f" --max-stake {max_stake}"
        f" --max-losses {max_losses}"
        f" --profit-target {profit_tgt}"
        f" --loss-limit {loss_limit}"
        f" --score-threshold {threshold}"
        f" --strategy {algo}"
        f" --trade-strategy {trade_strat}"
    )
    if sym_str:
        cmd += f" --symbols {sym_str}"
    cmd += f" --app-json {sess_file}"
    return cmd


def launch_runner(cfg: dict, algo: str, sess_file: Path, tmux_name: str) -> None:
    kill_tmux(tmux_name)
    cmd     = build_bot_command(cfg, algo, sess_file)
    script  = Path(f".launcher-bench-{algo}.sh")
    script.write_text(f"#!/bin/bash\n{cmd}\n")
    script.chmod(0o755)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, f"bash {script}"],
        capture_output=True,
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not CONFIG_FILE.exists():
        print("benchmark_config.json not found — start from the dashboard.", file=sys.stderr)
        sys.exit(1)

    cfg           = read_json(CONFIG_FILE)
    algos         = cfg.get("algos", [])
    profit_target = float(cfg.get("profit_target", 500))
    loss_limit    = float(cfg.get("loss_limit", cfg.get("capital_per_algo", 2500)))

    if not algos:
        print("No algorithms configured.", file=sys.stderr)
        sys.exit(1)

    # Build initial runner records
    runners = {}
    for algo in algos:
        rand_id    = random.randint(1000, 9999)
        sess_file  = DATA_DIR / f"bench-{algo}-{rand_id}-trades.json"
        tmux_name  = f"bbot-bench-{algo}"
        runners[algo] = {
            "status":           "starting",
            "tmux_session":     tmux_name,
            "session_file":     str(sess_file),
            "started_at":       None,
            "ended_at":         None,
            "end_reason":       None,
            "trades":           0,
            "wins":             0,
            "losses":           0,
            "net_pnl":          0.0,
            "win_rate":         0.0,
            "initial_equity":   None,
            "current_equity":   None,
            "max_win_streak":   0,
            "max_loss_streak":  0,
            "duration_seconds": 0,
        }

    state = {
        "status":       "running",
        "started_at":   time.time(),
        "config":       cfg,
        "runners":      runners,
        "completed_at": None,
    }
    write_json(STATE_FILE, state)

    # Launch all bots simultaneously
    now = time.time()
    for algo, r in runners.items():
        launch_runner(cfg, algo, Path(r["session_file"]), r["tmux_session"])
        r["status"]     = "running"
        r["started_at"] = now
        print(f"[benchmark] Launched {algo} → {r['session_file']}")

    write_json(STATE_FILE, state)

    # Monitor loop
    try:
        while True:
            time.sleep(POLL_SEC)

            for algo, r in runners.items():
                if r["status"] not in ("running", "starting"):
                    continue

                sess_path = Path(r["session_file"])
                snap      = snapshot_from_file(sess_path, profit_target, loss_limit)

                # Update live stats
                r.update({k: snap[k] for k in snap})

                still_alive = tmux_running(r["tmux_session"])

                # Decide end reason when tmux session dies
                if not still_alive:
                    net = snap["net_pnl"]
                    if net >= profit_target:
                        reason = "profit_reached"
                    elif net <= -loss_limit:
                        reason = "loss_limit_hit"
                    else:
                        reason = "stopped"
                    r["status"]     = "done"
                    r["ended_at"]   = time.time()
                    r["end_reason"] = reason
                    if r["started_at"]:
                        r["duration_seconds"] = int(time.time() - r["started_at"])
                    print(f"[benchmark] {algo} finished → {reason}  pnl={net:+.2f}")

            all_done = all(r["status"] == "done" for r in runners.values())
            if all_done:
                state["status"]       = "completed"
                state["completed_at"] = time.time()
                write_json(STATE_FILE, state)
                print("[benchmark] All runners finished.")
                break

            write_json(STATE_FILE, state)

    except KeyboardInterrupt:
        print("\n[benchmark] Interrupted — stopping all runners.")
        for algo, r in runners.items():
            if r["status"] in ("running", "starting"):
                kill_tmux(r["tmux_session"])
                r["status"]     = "stopped"
                r["ended_at"]   = time.time()
                r["end_reason"] = "manual_stop"
                if r["started_at"]:
                    r["duration_seconds"] = int(time.time() - r["started_at"])
        state["status"]       = "stopped"
        state["completed_at"] = time.time()
        write_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
