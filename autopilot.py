#!/usr/bin/env python3
"""
Systematic Autopilot — Disciplined Trading Manager

Acts like a professional human trader:

  • Picks the best algorithm using multi-source intelligence
    (recent autopilot history, optional demo benchmark, market scanner).
    No more "always novaburst": each algo earns its slot from real data.

  • Sizes stakes so a SINGLE winning trade hits the sprint TP, with
    martingale fallback for misses. Avoids the "1$ stake chasing 15$"
    death-march of dozens of trades that a long losing run can wipe out.

  • Runs short sprints, cools down between them (longer after losses),
    and stops once the daily profit goal is reached.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, List, Dict

DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "autopilot_config.json"
STATE_FILE = DATA_DIR / "autopilot_state.json"
RESULT_FILE = DATA_DIR / "autopilot_result.json"
SCAN_FILE = DATA_DIR / "market_scan.json"
HISTORY_FILE = DATA_DIR / "autopilot_history.json"
BENCH_STATE = DATA_DIR / "benchmark_state.json"
LOG_FILE = DATA_DIR / "autopilot.log"

# Deriv even/odd payout: stake → stake * 1.95 on win (profit = stake * 0.95).
PAYOUT_FACTOR = 0.95

# Truncate previous run's log so the UI shows ONLY the current session.
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    if LOG_FILE.exists():
        LOG_FILE.unlink()
except Exception:
    pass


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _FlushingFileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("autopilot")


# ─────────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────────
def load_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Error reading {path.name}: {e}")
    return None


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────
# Stake sizing — make one win count
# ─────────────────────────────────────────────────────────────────
def compute_stake(
    tp: float,
    martingale: float,
    stake_min: float,
    stake_max: float,
    sizing_mode: str,
    max_stake_cap: float,
) -> Tuple[float, float, int]:
    """Return (base_stake, effective_max_stake, planned_steps).

    Sizing modes:
      oneshot       → first win hits TP        (stake = TP / 0.95)
      twoshot       → 2 wins hit TP            (stake = TP / (2 * 0.95))
      conservative  → ~4 wins to hit TP        (stake = TP / (4 * 0.95))
      random        → random in [stake_min, stake_max] (legacy)

    The base stake is clamped into [stake_min, stake_max]. effective_max is
    the cap forwarded to bot.py (--max-stake), bounded by the user's config.
    """
    mode = (sizing_mode or "oneshot").lower()
    if mode == "oneshot":
        base = tp / PAYOUT_FACTOR
    elif mode == "twoshot":
        base = tp / (2.0 * PAYOUT_FACTOR)
    elif mode == "conservative":
        base = tp / (4.0 * PAYOUT_FACTOR)
    else:
        base = random.uniform(stake_min, stake_max)

    if mode != "random":
        base = max(stake_min, min(base, stake_max))
    base = round(base, 2)

    steps = 0
    s = base
    while s * martingale <= max_stake_cap and steps < 12:
        s *= martingale
        steps += 1
    effective_max = min(max_stake_cap, s)
    return base, round(effective_max, 2), steps


# ─────────────────────────────────────────────────────────────────
# Per-algorithm history
# ─────────────────────────────────────────────────────────────────
def load_history() -> dict:
    h = load_json(HISTORY_FILE) or {}
    h.setdefault("per_algo", {})
    h.setdefault("recent", [])
    return h


def save_history(h: dict) -> None:
    h["recent"] = h["recent"][-100:]
    write_json(HISTORY_FILE, h)


def update_history(h: dict, algo: str, net: float) -> None:
    rec = h["per_algo"].setdefault(
        algo,
        {"sprints": 0, "wins": 0, "losses": 0, "net_total": 0.0, "last_used": 0.0},
    )
    rec["sprints"] += 1
    rec["net_total"] += net
    rec["last_used"] = time.time()
    if net > 0:
        rec["wins"] += 1
    else:
        rec["losses"] += 1
    h["recent"].append({"algo": algo, "net": net, "ts": time.time()})


# ─────────────────────────────────────────────────────────────────
# Multi-source algorithm scoring
# ─────────────────────────────────────────────────────────────────
def algo_score_from_history(history: dict, algo: str) -> Tuple[float, str]:
    rec = history["per_algo"].get(algo)
    if not rec or rec["sprints"] == 0:
        return 0.5, "no history"

    wr = rec["wins"] / rec["sprints"]
    avg_net = rec["net_total"] / rec["sprints"]

    recent_with_algo = [r for r in history["recent"][-30:] if r["algo"] == algo][-5:]
    streak = 0
    for r in reversed(recent_with_algo):
        if r["net"] <= 0:
            streak += 1
        else:
            break

    score = wr * 0.5 + max(0.0, min(1.0, (avg_net + 20.0) / 40.0)) * 0.5
    if streak >= 3:
        score *= 0.3
        return score, f"hist wr={wr:.0%} avg=${avg_net:+.1f} streak={streak} (penalised)"
    if streak == 2:
        score *= 0.7
    return score, f"hist wr={wr:.0%} avg=${avg_net:+.1f} streak={streak}"


def algo_score_from_benchmark(algo: str) -> Tuple[float, str]:
    bs = load_json(BENCH_STATE)
    if not bs or "runners" not in bs:
        return 0.0, "no benchmark"
    r = bs["runners"].get(algo)
    if not r:
        return 0.0, "no benchmark"
    trades = r.get("trades", 0)
    if trades < 3:
        return 0.0, f"bench too short ({trades})"
    net = r.get("net_pnl", 0.0)
    wr = r.get("win_rate", 0.0)
    score = wr * 0.5 + max(0.0, min(1.0, (net + 30.0) / 60.0)) * 0.5
    return score, f"bench wr={wr:.0%} net=${net:+.1f}"


def algo_score_from_scanner(algo: str, scan_data: Optional[dict]) -> Tuple[float, str]:
    if not scan_data or "results" not in scan_data:
        return 0.0, "no scan"
    best = 0.0
    for d in scan_data["results"]:
        vol = d.get("volatility", {}).get("level", "UNKNOWN")
        if vol == "EXTREME":
            continue
        dg = d.get("digits", {})
        p = d.get("patterns", {})
        ps = p.get("pulse", {}).get("score", 0.0)
        bias = dg.get("bias_magnitude", 0.0)
        if algo in ("pulse", "ensemble", "novaburst", "adaptive", "aegis"):
            if dg.get("is_biased") and ps >= 0.55:
                best = max(best, ps)
        elif algo == "alphabloom":
            if dg.get("is_biased") and bias >= 0.08:
                best = max(best, min(1.0, bias * 5.0))
    return best, f"scan peak={best:.2f}"


def select_algo(
    allowed: List[str],
    history: dict,
    scan_data: Optional[dict],
    use_benchmark: bool,
    weights: Dict[str, float],
) -> Tuple[str, str]:
    if not allowed:
        return "adaptive", "no allowed algos → fallback adaptive"

    rows: List[Tuple[str, float, str]] = []
    for a in allowed:
        h_score, h_expl = algo_score_from_history(history, a)
        if use_benchmark:
            b_score, b_expl = algo_score_from_benchmark(a)
        else:
            b_score, b_expl = 0.0, "skip"
        s_score, s_expl = algo_score_from_scanner(a, scan_data)

        w_h = weights.get("history", 0.5)
        w_b = weights.get("benchmark", 0.3) if use_benchmark else 0.0
        w_s = weights.get("scanner", 0.2)
        total = w_h + w_b + w_s or 1.0
        w_h, w_b, w_s = w_h / total, w_b / total, w_s / total

        composite = h_score * w_h + b_score * w_b + s_score * w_s
        rows.append((a, composite, f"{h_expl} | {b_expl} | {s_expl} | composite={composite:.3f}"))

    rows.sort(key=lambda r: r[1], reverse=True)

    # ε-greedy exploration: occasionally pick the runner-up.
    if len(rows) > 1 and random.random() < 0.15:
        a, _, expl = rows[1]
        return a, f"EXPLORE {a} → {expl}"
    a, _, expl = rows[0]
    return a, f"BEST {a} → {expl}"


# ─────────────────────────────────────────────────────────────────
# Optional benchmark warm-up
# ─────────────────────────────────────────────────────────────────
def run_benchmark(cfg: dict) -> None:
    bench_cfg = {
        "token": cfg.get("token", ""),
        "account_mode": "demo",
        "algos": cfg.get("allowed_algos", ["pulse", "novaburst", "adaptive"]),
        "base_stake": 1.0,
        "martingale": cfg.get("martingale", 2.2),
        "max_stake": cfg.get("max_stake", 50.0),
        "profit_target": 100,
        "loss_limit": -100,
        "trade_strategy": cfg.get("trade_strategy", "even_odd"),
    }
    write_json(DATA_DIR / "benchmark_config.json", bench_cfg)

    log.info("Starting demo benchmark to evaluate algorithms...")
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "bbot-benchmark", "python3 benchmark.py"],
        capture_output=True,
    )
    wait_mins = float(cfg.get("benchmark_duration_minutes", 5.0))
    log.info(f"Benchmark running for {wait_mins:.1f} min...")
    time.sleep(max(60.0, wait_mins * 60.0))
    log.info("Stopping benchmark.")
    subprocess.run(["tmux", "kill-session", "-t", "=bbot-benchmark"], capture_output=True)
    subprocess.run(["pkill", "-f", "bot.py.*bench-"], capture_output=True)


# ─────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 60)
    log.info("Systematic Autopilot starting")
    log.info("=" * 60)

    cumulative_profit = 0.0
    sprint_count = 0
    benchmark_done = False
    history = load_history()
    session_started_at = time.time()
    sprints: List[dict] = []

    while True:
        cfg = load_json(CONFIG_FILE)
        if not cfg:
            log.info("Waiting for autopilot_config.json...")
            time.sleep(5)
            continue

        max_daily = float(cfg.get("max_daily_profit", 100.0))
        if cumulative_profit >= max_daily:
            log.info(f"GOAL REACHED — cumulative ${cumulative_profit:.2f} >= ${max_daily:.2f}")
            write_json(STATE_FILE, {
                "cumulative_profit": cumulative_profit,
                "max_daily_profit": max_daily,
                "sprint_count": sprint_count,
                "current_algo": "DONE",
                "status": "GOAL REACHED",
                "updated_at": time.time(),
            })
            write_json(RESULT_FILE, {
                "session_started_at": round(session_started_at),
                "last_updated": round(time.time()),
                "max_daily_profit": max_daily,
                "cumulative_profit": round(cumulative_profit, 2),
                "sprints_completed": sprint_count,
                "total_trades": sum(s["trades"] for s in sprints),
                "total_wins": sum(s["wins"] for s in sprints),
                "total_losses": sum(s["losses"] for s in sprints),
                "overall_win_rate": round(
                    sum(s["wins"] for s in sprints) / max(1, sum(s["trades"] for s in sprints)), 4
                ),
                "status": "GOAL REACHED",
                "sprints": sprints,
            })
            break

        sprint_count += 1
        log.info("")
        log.info(f"━━━━━━━━━━ Sprint #{sprint_count} ━━━━━━━━━━")
        log.info(f"Cumulative: ${cumulative_profit:+.2f} / ${max_daily:.2f}")

        allowed = cfg.get("allowed_algos") or [
            "adaptive", "pulse", "novaburst", "ensemble", "alphabloom"
        ]
        use_bench = bool(cfg.get("use_benchmark", False))
        if use_bench and not benchmark_done:
            run_benchmark(cfg)
            benchmark_done = True

        scan_data = load_json(SCAN_FILE)
        weights = cfg.get("selection_weights") or {
            "history": 0.5, "benchmark": 0.3, "scanner": 0.2,
        }
        algo, reasoning = select_algo(allowed, history, scan_data, use_bench, weights)
        ts = cfg.get("trade_strategy", "even_odd")

        # ── Stake sizing ──
        tp_min, tp_max = cfg.get("sprint_tp_range", [5.0, 15.0])
        sl_min, sl_max = cfg.get("sprint_sl_range", [-50.0, -20.0])
        stake_min, stake_max = cfg.get("stake_range", [0.5, 2.0])
        sizing_mode = cfg.get("sizing_mode", "oneshot")
        martingale = float(cfg.get("martingale", 2.2))
        max_stake_cap = float(cfg.get("max_stake", 50.0))

        tp = round(random.uniform(min(tp_min, tp_max), max(tp_min, tp_max)), 2)
        sl = round(random.uniform(min(sl_min, sl_max), max(sl_min, sl_max)), 2)
        base_stake, effective_max, steps = compute_stake(
            tp, martingale, stake_min, stake_max, sizing_mode, max_stake_cap,
        )

        log.info(f"Algorithm: {algo}")
        log.info(f"  reason: {reasoning}")
        log.info(f"Sizing: mode={sizing_mode} base=${base_stake:.2f} → 1 win = +${base_stake * PAYOUT_FACTOR:.2f}")
        log.info(f"Sprint TP=+${tp:.2f}  SL=${sl:.2f}  martingale=x{martingale} (~{steps} steps to ${effective_max:.2f})")

        # ── Build bot.py command ──
        app_json = DATA_DIR / "autopilot_sprint.json"
        cmd = [
            "python3", "bot.py",
            "--token", cfg.get("token", ""),
            "--account-mode", cfg.get("account_mode", "demo"),
            "--strategy", algo,
            "--trade-strategy", ts,
            "--base-stake", str(base_stake),
            "--profit-target", str(tp),
            "--loss-limit", str(sl),
            "--martingale", str(martingale),
            "--max-stake", str(effective_max),
            "--app-json", str(app_json),
            "--disable-hot-reload",
        ]
        flag_msgs: List[str] = []
        if cfg.get("disable_kelly"):
            cmd.append("--disable-kelly")
            flag_msgs.append("--disable-kelly")
        if cfg.get("disable_risk"):
            cmd.append("--disable-risk-engine")
            flag_msgs.append("--disable-risk-engine")
        if cfg.get("ml_filter"):
            cmd.append("--ml-filter")
            ml_thr = cfg.get("ml_threshold")
            if ml_thr not in (None, "", False):
                cmd += ["--ml-threshold", str(ml_thr)]
                flag_msgs.append(f"--ml-filter (thr={ml_thr})")
            else:
                flag_msgs.append("--ml-filter")
        if flag_msgs:
            log.info("Flags: " + " | ".join(flag_msgs))

        symbols = cfg.get("symbols") or [
            "R_10", "R_25", "R_50", "R_75", "R_100",
            "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
        ]
        cmd += ["--symbols"] + symbols

        write_json(STATE_FILE, {
            "cumulative_profit": cumulative_profit,
            "max_daily_profit": max_daily,
            "sprint_count": sprint_count,
            "current_algo": algo,
            "current_stake": base_stake,
            "current_tp": tp,
            "current_sl": sl,
            "sizing_mode": sizing_mode,
            "status": f"SPRINT #{sprint_count} — {algo}",
            "updated_at": time.time(),
        })

        # ── Run sprint ──
        bot_log = DATA_DIR / "autopilot_bot.log"
        sprint_started_at = time.time()
        log.info("Sprint live — bot running...")
        with open(bot_log, "w", encoding="utf-8") as f:
            f.write(f"=== SPRINT #{sprint_count} | {algo} | base=${base_stake:.2f} | tp=${tp} ===\n")
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
        sprint_ended_at = time.time()
        log.info(f"Sprint exit code: {proc.returncode}")

        # ── Resolve sprint result ──
        sprint_net = 0.0
        trades = wins = 0
        sd = load_json(app_json)
        if sd and "summary" in sd:
            sprint_net = float(sd["summary"].get("net_pnl", 0.0))
            trades = int(sd["summary"].get("trade_count", 0))
            wins = int(sd["summary"].get("wins", 0))
        losses = trades - wins
        wr = round(wins / trades, 4) if trades > 0 else 0.0
        duration_s = int(sprint_ended_at - sprint_started_at)
        log.info(f"Result: trades={trades}  wins={wins}  losses={losses}  wr={wr:.0%}  net=${sprint_net:+.2f}  duration={duration_s}s")

        cumulative_profit += sprint_net
        update_history(history, algo, sprint_net)
        save_history(history)

        sprints.append({
            "sprint": sprint_count,
            "algo": algo,
            "trade_strategy": ts,
            "sizing_mode": sizing_mode,
            "base_stake": base_stake,
            "tp": tp,
            "sl": sl,
            "started_at": round(sprint_started_at),
            "ended_at": round(sprint_ended_at),
            "duration_s": duration_s,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": wr,
            "net_pnl": round(sprint_net, 2),
            "cumulative_after": round(cumulative_profit, 2),
        })

        write_json(RESULT_FILE, {
            "session_started_at": round(session_started_at),
            "last_updated": round(time.time()),
            "max_daily_profit": max_daily,
            "cumulative_profit": round(cumulative_profit, 2),
            "sprints_completed": sprint_count,
            "total_trades": sum(s["trades"] for s in sprints),
            "total_wins": sum(s["wins"] for s in sprints),
            "total_losses": sum(s["losses"] for s in sprints),
            "overall_win_rate": round(
                sum(s["wins"] for s in sprints) / max(1, sum(s["trades"] for s in sprints)), 4
            ),
            "status": "RUNNING",
            "sprints": sprints,
        })

        write_json(STATE_FILE, {
            "cumulative_profit": cumulative_profit,
            "max_daily_profit": max_daily,
            "sprint_count": sprint_count,
            "current_algo": "COOLDOWN",
            "last_sprint_pnl": sprint_net,
            "status": "COOLDOWN",
            "updated_at": time.time(),
        })

        if cumulative_profit >= max_daily:
            continue

        if sprint_net > 0:
            cd = float(cfg.get("cooldown_win_minutes", 2.0))
            log.info(f"Win — cooling down {cd:.1f} min before next sprint.")
        else:
            cd = float(cfg.get("cooldown_loss_minutes", 5.0))
            log.info(f"Loss — cooling down {cd:.1f} min to let market reset.")
        time.sleep(max(0.0, cd * 60.0))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Autopilot stopped by user.")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
