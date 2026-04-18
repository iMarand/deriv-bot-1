"""
Offline ML Filter Trainer

Reads all trade logs in data/*-trades.json, builds per-trade feature vectors from
metadata only (symbol, contract, time, recent rolling win-rates, martingale depth),
and fits a P(win) classifier. The trained artifact is used at runtime by ml_filter.py
to *gate* candidate signals produced by alphabloom / pulse / ensemble.

Usage:
    py train_filter.py
    py train_filter.py --model logreg --test-frac 0.2 --threshold 0.55

Output:
    data/ml_filter.pkl     — pickled dict with model + feature schema + threshold
    data/ml_filter.txt     — human-readable training report
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from collections import deque
from pathlib import Path
from typing import List, Tuple

import numpy as np

from ml_features import FeatureBuilder, FEATURE_NAMES

DATA_DIR = Path(__file__).parent / "data"
MODEL_OUT = DATA_DIR / "ml_filter.pkl"
REPORT_OUT = DATA_DIR / "ml_filter.txt"
META_OUT = DATA_DIR / "ml_filter.json"


def load_trades(include: List[str] | None = None) -> Tuple[List[dict], List[str]]:
    """Load every trade from every *-trades.json, sorted by timestamp.

    If `include` is provided, only those filenames (basename) are loaded.
    Returns (trades, list of files actually used).
    """
    trades: List[dict] = []
    used: List[str] = []
    all_files = sorted(DATA_DIR.glob("*.json"))
    # Never consume our own output
    all_files = [f for f in all_files if f.name != META_OUT.name]
    if include:
        want = set(include)
        files = [f for f in all_files if f.name in want]
    else:
        files = all_files
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  skip {f.name}: {e}")
            continue
        ts = d.get("trades") or []
        kept = 0
        for t in ts:
            if not t.get("symbol") or not t.get("contract_type") or "result" not in t:
                continue
            trades.append(t)
            kept += 1
        if kept:
            used.append(f.name)
        print(f"  {f.name}: {len(ts)} trades ({kept} labeled)")
    trades.sort(key=lambda t: t.get("timestamp", 0))
    return trades, used


def build_dataset(trades: List[dict]) -> Tuple[np.ndarray, np.ndarray]:
    """Replay trades in time order, building features per trade."""
    fb = FeatureBuilder()
    X_rows = []
    y_rows = []
    for t in trades:
        symbol = t["symbol"]
        contract_type = t["contract_type"]
        ts = float(t.get("timestamp", 0))
        is_win = t["result"] == "win"

        feat = fb.features_for(symbol, contract_type, ts)
        X_rows.append(feat)
        y_rows.append(1 if is_win else 0)

        fb.record(symbol, contract_type, ts, is_win)

    X = np.array(X_rows, dtype=np.float32)
    y = np.array(y_rows, dtype=np.int32)
    return X, y


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["logreg", "gbm"], default="gbm")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="P(win) cutoff used at runtime to gate trades")
    ap.add_argument("--min-trades", type=int, default=300,
                    help="Bail out if fewer labeled trades than this")
    ap.add_argument("--include", type=str, default="",
                    help="Comma-separated list of data/ filenames to use (default: all)")
    args = ap.parse_args()

    include = [s.strip() for s in args.include.split(",") if s.strip()] or None

    print(f"Scanning {DATA_DIR} for trade logs...")
    trades, used_files = load_trades(include)
    print(f"Total trades loaded: {len(trades)}")
    if len(trades) < args.min_trades:
        print(f"ERROR: need at least {args.min_trades} trades, have {len(trades)}")
        return 1

    print("Building features...")
    X, y = build_dataset(trades)
    print(f"  X shape {X.shape} | overall win rate {y.mean():.3f}")

    # Time-based split (no leakage)
    split = int(len(X) * (1.0 - args.test_frac))
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]
    print(f"  train {len(X_tr)} | test {len(X_te)}")

    # Train
    print(f"Training model={args.model}...")
    if args.model == "logreg":
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)
        clf = LogisticRegression(max_iter=500, class_weight="balanced")
        clf.fit(X_tr_s, y_tr)
        model_bundle = {"kind": "logreg", "scaler": scaler, "clf": clf}
        p_tr = clf.predict_proba(X_tr_s)[:, 1]
        p_te = clf.predict_proba(X_te_s)[:, 1]
    else:
        from sklearn.ensemble import GradientBoostingClassifier
        clf = GradientBoostingClassifier(
            n_estimators=200, max_depth=3, learning_rate=0.05,
            subsample=0.9, random_state=42,
        )
        clf.fit(X_tr, y_tr)
        model_bundle = {"kind": "gbm", "clf": clf}
        p_tr = clf.predict_proba(X_tr)[:, 1]
        p_te = clf.predict_proba(X_te)[:, 1]

    # Evaluate
    from sklearn.metrics import roc_auc_score, accuracy_score

    def report(name, p, y):
        lines = []
        data = {"base_wr": float(y.mean()), "auc": None, "thresholds": []}
        lines.append(f"  {name}: base WR {y.mean():.3f}")
        try:
            auc = roc_auc_score(y, p)
            data["auc"] = float(auc)
        except Exception:
            auc = float("nan")
        lines.append(f"    ROC-AUC: {auc:.4f}  (0.50 = no edge, >0.55 = useful)")
        for thr in (0.50, 0.52, 0.55, 0.58, 0.60):
            mask = p >= thr
            n = int(mask.sum())
            if n == 0:
                lines.append(f"    p>={thr:.2f}: 0 trades")
                data["thresholds"].append({"p": thr, "n": 0, "keep_frac": 0.0, "wr": None})
                continue
            wr = float(y[mask].mean())
            frac = n / len(y)
            lines.append(f"    p>={thr:.2f}: keep {frac:.1%} ({n} trades), WR {wr:.3f}")
            data["thresholds"].append({"p": thr, "n": n, "keep_frac": frac, "wr": wr})
        return "\n".join(lines), data

    tr_report, tr_data = report("TRAIN", p_tr, y_tr)
    te_report, te_data = report("TEST ", p_te, y_te)
    print(tr_report)
    print(te_report)

    # Save
    bundle = {
        "version": 1,
        "created_at": time.time(),
        "feature_names": FEATURE_NAMES,
        "threshold": args.threshold,
        "model": model_bundle,
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "base_wr_train": float(y_tr.mean()),
        "base_wr_test": float(y_te.mean()),
    }
    DATA_DIR.mkdir(exist_ok=True)
    with open(MODEL_OUT, "wb") as f:
        pickle.dump(bundle, f)

    with open(REPORT_OUT, "w", encoding="utf-8") as f:
        f.write(f"ml_filter — model={args.model} threshold={args.threshold}\n")
        f.write(f"trades_total={len(trades)} train={len(X_tr)} test={len(X_te)}\n\n")
        f.write(tr_report + "\n\n")
        f.write(te_report + "\n")

    meta = {
        "version": 1,
        "created_at": bundle["created_at"],
        "model_kind": args.model,
        "threshold": args.threshold,
        "trades_total": len(trades),
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "files_used": used_files,
        "train": tr_data,
        "test": te_data,
        "report_text": f"{tr_report}\n\n{te_report}",
    }
    with open(META_OUT, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\nSaved {MODEL_OUT}")
    print(f"Saved {REPORT_OUT}")
    print(f"Saved {META_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
