#!/usr/bin/env python3
"""
Benchmark Orchestrator — runs multiple algorithms simultaneously and compares results.

Config read from : data/benchmark_config.json  (written by analysis.php)
State written to : data/benchmark_state.json    (polled by analysis.php)

Each algorithm gets its own tmux session (bbot-bench-<algo>) and its own
session JSON file.  The orchestrator monitors every 5 s and shuts down any
bot whose profit_target / loss_limit has been reached.
When all runners are done it writes a final comparison and exits.
"""

import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR    = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "benchmark_config.json"
STATE_FILE  = DATA_DIR / "benchmark_state.json"
LOG_FILE    = DATA_DIR / "benchmark_orchestrator.log"

POLL_SEC    = 5
# How long to wait before treating a dead session as truly stopped
# (gives the bot time to finish WebSocket handshake + first tick subscription)
GRACE_SEC   = 90

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("benchmark")


# ── helpers ──────────────────────────────────────────────────────────────────

def tmux_running(name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True,
    )
    return r.returncode == 0


def kill_tmux(name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={name}"],
        capture_output=True,
    )


def tmux_logs(name: str, lines: int = 300) -> str:
    """Capture last N lines from a tmux pane."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", f"={name}", "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
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


def snapshot_from_file(path: Path) -> dict:
    data    = read_json(path) or {}
    summary = data.get("summary", {})
    session = data.get("session", {})
    trades  = data.get("trades", [])
    mw, ml  = compute_streaks(trades)
    started = session.get("started_at")
    updated = session.get("updated_at")
    duration = int(updated - started) if (started and updated) else 0
    return {
        "trades":           summary.get("trade_count", 0),
        "wins":             summary.get("wins", 0),
        "losses":           summary.get("losses", 0),
        "net_pnl":          round(summary.get("net_pnl", 0.0), 4),
        "win_rate":         round(summary.get("win_rate", 0.0), 4),
        "initial_equity":   session.get("initial_equity"),
        "current_equity":   session.get("current_equity"),
        "max_win_streak":   mw,
        "max_loss_streak":  ml,
        "duration_seconds": duration,
    }


# ── launcher ─────────────────────────────────────────────────────────────────

def build_bot_command(cfg: dict, algo: str, sess_file: Path) -> str:
    # Use forward slashes so the bash script works on all platforms
    sess_path_str = sess_file.as_posix()

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

    parts = [
        "python3", "bot.py",
        "--token", token,
        "--account-mode", mode,
        "--app-id", str(app_id),
        "--base-stake", str(base_stake),
        "--martingale", str(martingale),
        "--max-stake", str(max_stake),
        "--max-losses", str(max_losses),
        "--profit-target", str(profit_tgt),
        "--loss-limit", str(loss_limit),
        "--score-threshold", str(threshold),
        "--strategy", algo,
        "--trade-strategy", trade_strat,
    ]
    if sym_str:
        parts += ["--symbols"] + symbols
    parts += ["--app-json", sess_path_str]

    return " ".join(parts)


def log_file_for(algo: str) -> Path:
    return DATA_DIR / f"bench-{algo}-bot.log"


def launch_runner(cfg: dict, algo: str, sess_file: Path, tmux_name: str) -> None:
    kill_tmux(tmux_name)
    time.sleep(0.3)

    bot_cmd  = build_bot_command(cfg, algo, sess_file)
    log_path = log_file_for(algo).as_posix()
    bot_dir  = Path(__file__).parent.as_posix()

    # The launcher:
    #  1. Redirects all bot output to a log file (persists after the pane dies)
    #  2. Keeps the tmux pane alive via `tail -f` so we can still capture it
    script_body = (
        f"#!/bin/bash\n"
        f"cd {bot_dir}\n"
        f"LOG={log_path}\n"
        f'echo "=== [{algo}] started at $(date) ===" > "$LOG"\n'
        f"{bot_cmd} >> \"$LOG\" 2>&1\n"
        f'EXIT_CODE=$?\n'
        f'echo "" >> "$LOG"\n'
        f'echo "=== [{algo}] EXITED (code $EXIT_CODE) at $(date) ===" >> "$LOG"\n'
        f'echo ""\n'
        f'echo "--- [{algo}] bot stopped (exit $EXIT_CODE) --- tail-following log ---"\n'
        f'tail -f "$LOG"\n'   # keeps pane alive; tmux_running() stays True
    )
    script = Path(__file__).parent / f".launcher-bench-{algo}.sh"
    script.write_text(script_body, encoding="utf-8")

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, f"bash {script.as_posix()}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Failed to launch %s: %s", tmux_name, result.stderr.strip())
    else:
        log.info("Launched %s → tmux:%s  log:%s", algo, tmux_name, log_path)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not CONFIG_FILE.exists():
        log.error("benchmark_config.json not found — start from the dashboard.")
        sys.exit(1)

    cfg           = read_json(CONFIG_FILE)
    algos         = cfg.get("algos", [])
    profit_target = float(cfg.get("profit_target", 500))
    loss_limit    = float(cfg.get("loss_limit", cfg.get("capital_per_algo", 2500)))

    if not algos:
        log.error("No algorithms configured.")
        sys.exit(1)

    log.info("Starting benchmark: algos=%s  profit_target=%s  loss_limit=%s",
             algos, profit_target, loss_limit)

    # ── build initial runner records ──────────────────────────────────────────
    runners = {}
    for algo in algos:
        rand_id    = random.randint(1000, 9999)
        sess_file  = DATA_DIR / f"bench-{algo}-{rand_id}-trades.json"
        tmux_name  = f"bbot-bench-{algo}"
        runners[algo] = {
            "status":           "starting",
            "tmux_session":     tmux_name,
            "session_file":     sess_file.as_posix(),
            "log_file":         log_file_for(algo).as_posix(),
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

    # ── launch all bots simultaneously ────────────────────────────────────────
    now = time.time()
    for algo, r in runners.items():
        launch_runner(cfg, algo, Path(r["session_file"]), r["tmux_session"])
        r["status"]     = "running"
        r["started_at"] = now

    write_json(STATE_FILE, state)
    log.info("All runners launched. Monitoring every %ds …", POLL_SEC)

    # ── monitor loop ──────────────────────────────────────────────────────────
    try:
        while True:
            time.sleep(POLL_SEC)

            for algo, r in runners.items():
                if r["status"] not in ("running", "starting"):
                    continue

                sess_path   = Path(r["session_file"])
                snap        = snapshot_from_file(sess_path)
                r.update({k: snap[k] for k in snap})

                elapsed  = time.time() - (r["started_at"] or time.time())
                log_path = Path(r["log_file"])

                # Detect bot exit by looking for the "EXITED" marker in its log file.
                # (The tmux pane stays alive via `tail -f`, so pane presence is not reliable.)
                bot_exited = False
                if log_path.exists():
                    try:
                        tail = log_path.read_text(encoding="utf-8", errors="replace")
                        if "=== EXITED" in tail or "=== [" + algo + "] EXITED" in tail:
                            bot_exited = True
                    except OSError:
                        pass

                # Fallback: if tmux pane itself died (tail -f got killed externally)
                if not bot_exited and not tmux_running(r["tmux_session"]):
                    if elapsed < GRACE_SEC:
                        log.warning(
                            "%s tmux not visible yet (%.0fs, grace=%ds) — waiting…",
                            algo, elapsed, GRACE_SEC,
                        )
                        continue
                    bot_exited = True

                if bot_exited:
                    net = snap["net_pnl"]
                    if net >= profit_target:
                        reason = "profit_reached"
                    elif net <= -loss_limit:
                        reason = "loss_limit_hit"
                    else:
                        reason = "stopped"
                    r["status"]           = "done"
                    r["ended_at"]         = time.time()
                    r["end_reason"]       = reason
                    r["duration_seconds"] = int(elapsed)
                    # Kill the tail -f pane now that bot is done
                    kill_tmux(r["tmux_session"])
                    log.info("%s finished → %s  pnl=%+.2f  trades=%d",
                             algo, reason, net, snap["trades"])
                else:
                    net = snap["net_pnl"]
                    log.debug("%s alive  pnl=%+.2f  trades=%d  elapsed=%.0fs",
                              algo, net, snap["trades"], elapsed)

            all_done = all(r["status"] == "done" for r in runners.values())
            if all_done:
                state["status"]       = "completed"
                state["completed_at"] = time.time()
                write_json(STATE_FILE, state)
                log.info("All runners finished — benchmark complete.")
                break

            write_json(STATE_FILE, state)

    except KeyboardInterrupt:
        log.info("Interrupted — stopping all runners.")
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
