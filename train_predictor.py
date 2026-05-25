#!/usr/bin/env python3
"""
Train and evaluate the XGBoost + CatBoost ensemble predictor.

Evaluation: walk-forward (time-series) cross-validation — train always
precedes test in time, no shuffling.

Metrics per threshold:
  AUC-ROC         overall discriminative power  (0.50 = random)
  PR-AUC          precision-recall area          (better for rare events)
  Lift@20%        precision improvement in top 20% vs random
  Cal.Error       mean |predicted_p - actual_p| across decile bins
  EV delta        expected-value gain when only betting high-confidence rounds

Usage:
    source .venv/bin/activate
    python train_predictor.py [--folds N] [--save] [--quiet]
"""

import argparse
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))

from predictor import THRESHOLDS, LAG_N, load_crashes, build_feature_matrix, CrashPredictor


# ── Metrics ───────────────────────────────────────────────────────────────────

def _auc_roc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, s)) if len(np.unique(y)) == 2 else float("nan")


def _pr_auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import average_precision_score
    return float(average_precision_score(y, s)) if len(np.unique(y)) == 2 else float("nan")


def _lift_at_k(y: np.ndarray, s: np.ndarray, k: float = 0.20) -> float:
    n = max(1, int(len(s) * k))
    top = np.argsort(s)[::-1][:n]
    base = y.mean()
    return float(y[top].mean() / max(base, 1e-9))


def _cal_error(y: np.ndarray, s: np.ndarray, n_bins: int = 10) -> float:
    bins = np.unique(np.percentile(s, np.linspace(0, 100, n_bins + 1)))
    errs = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (s >= lo) & (s <= hi)
        if m.sum():
            errs.append(abs(s[m].mean() - y[m].mean()))
    return float(np.mean(errs)) if errs else float("nan")


# ── Walk-forward CV — single pass ─────────────────────────────────────────────

def walk_forward_cv(
    crashes: np.ndarray,
    n_folds: int = 5,
    verbose: bool = True,
) -> Tuple[Dict, Dict, Dict]:
    """
    Returns
    -------
    fold_metrics : {threshold: {metric: [fold_values]}}
    oof_y        : {threshold: ndarray}  out-of-fold true labels
    oof_p        : {threshold: ndarray}  out-of-fold ensemble probabilities
    """
    try:
        import xgboost as xgb
    except ImportError as e:
        raise ImportError(f"Missing: {e}. Run: pip install xgboost")
    from sklearn.ensemble import HistGradientBoostingClassifier as HGBC

    fold_metrics: Dict = {t: {"auc": [], "pr_auc": [], "lift20": [], "cal_err": []}
                          for t in THRESHOLDS}
    oof_y: Dict = {t: [] for t in THRESHOLDS}
    oof_p: Dict = {t: [] for t in THRESHOLDS}

    n = len(crashes)
    fold_size = n // n_folds

    for fold in range(1, n_folds):
        train_end = fold * fold_size
        test_end  = (fold + 1) * fold_size if fold < n_folds - 1 else n

        train_crashes = crashes[:train_end]
        # include LAG_N overlap so test features are aligned
        seg   = crashes[max(0, train_end - LAG_N) : test_end]
        skip  = min(LAG_N, train_end)   # rows in seg that are pure history

        if len(train_crashes) < LAG_N * 3:
            if verbose:
                print(f"  Fold {fold}: skipped (too little training data)")
            continue

        X_tr, y_tr_d, _ = build_feature_matrix(train_crashes)
        X_sg, y_sg_d, _ = build_feature_matrix(seg)
        X_te = X_sg[skip:]
        y_te_d = {t: y_sg_d[t][skip:] for t in THRESHOLDS}

        if len(X_te) == 0:
            continue

        if verbose:
            print(f"  Fold {fold}/{n_folds - 1}  train={len(X_tr):,}  test={len(X_te):,}",
                  flush=True)

        for t in THRESHOLDS:
            y_tr = y_tr_d[t]
            y_te = y_te_d[t]
            pr   = float(y_tr.mean())
            if pr in (0.0, 1.0):
                continue
            spw = (1.0 - pr) / max(pr, 1e-9)

            xm = xgb.XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
                scale_pos_weight=spw, eval_metric="logloss",
                tree_method="hist", random_state=42, n_jobs=1, verbosity=0,
            )
            xm.fit(X_tr, y_tr)

            hm = HGBC(
                max_iter=200, max_depth=4, learning_rate=0.05,
                min_samples_leaf=10, class_weight={0: 1.0, 1: spw},
                random_state=42,
            )
            hm.fit(X_tr, y_tr)

            p = (np.array(xm.predict_proba(X_te)[:, 1]) +
                 np.array(hm.predict_proba(X_te)[:, 1])) / 2.0

            fold_metrics[t]["auc"].append(_auc_roc(y_te, p))
            fold_metrics[t]["pr_auc"].append(_pr_auc(y_te, p))
            fold_metrics[t]["lift20"].append(_lift_at_k(y_te, p))
            fold_metrics[t]["cal_err"].append(_cal_error(y_te, p))

            oof_y[t].extend(y_te.tolist())
            oof_p[t].extend(p.tolist())

    return (
        fold_metrics,
        {t: np.array(v) for t, v in oof_y.items()},
        {t: np.array(v) for t, v in oof_p.items()},
    )


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(crashes: np.ndarray, fold_metrics: Dict, oof_y: Dict, oof_p: Dict):
    _, y_all_d, _ = build_feature_matrix(crashes)

    W = 78
    print("\n" + "=" * W)
    print("ENSEMBLE PREDICTOR (XGBoost + CatBoost) — WALK-FORWARD CV RESULTS")
    print(f"Rounds: {len(crashes):,}   Feature samples: {len(crashes)-LAG_N:,}   Lags: {LAG_N}")
    print("=" * W)

    hdr = f"{'Threshold':>10}  {'Base%':>6}  {'AUC-ROC':>8}  {'PR-AUC':>7}  {'Lift@20%':>9}  {'CalErr':>7}"
    print(hdr)
    print("-" * W)

    for t in THRESHOLDS:
        base  = float(y_all_d[t].mean())
        aucs  = [v for v in fold_metrics[t]["auc"]    if not np.isnan(v)]
        prs   = [v for v in fold_metrics[t]["pr_auc"] if not np.isnan(v)]
        lifts = [v for v in fold_metrics[t]["lift20"] if not np.isnan(v)]
        cals  = [v for v in fold_metrics[t]["cal_err"]if not np.isnan(v)]

        def s(arr, fmt=".4f"):
            return f"{np.mean(arr):{fmt}}" if arr else "   n/a"

        flag = ""
        if aucs:
            m = np.mean(aucs)
            if m > 0.54:
                flag = "  ◄ EDGE"
            elif m > 0.52:
                flag = "  · slight"

        print(f"  >= {t:4.1f}x  {base*100:5.2f}%  "
              f"{s(aucs):>8}  {s(prs):>7}  {s(lifts, '.3f'):>7}×  {s(cals):>7}{flag}")

    print("=" * W)
    print("  AUC  0.50=random  0.52=slight  0.55=useful ◄  0.60=strong (rare)")

    # ── EV analysis ──────────────────────────────────────────────────────────
    print("\n" + "-" * W)
    print("EXPECTED VALUE — model-gated betting vs always-bet (OOF predictions)")
    print(f"{'Threshold':>10}  {'Base rate':>10}  {'EV always':>10}  "
          f"{'EV @ p≥0.5':>11}  {'Coverage':>9}  {'Delta EV':>9}")
    print("-" * W)

    for t in [2.5, 3.5, 7.0, 8.0]:
        y = oof_y[t]
        p = oof_p[t]
        if len(y) == 0:
            continue
        base     = float(y.mean())
        ev_all   = base * (t - 1) - (1 - base)
        cutoff   = max(base * 1.05, 0.5 * base + 0.5)   # adaptive: 5% above base rate
        mask     = p >= cutoff
        if mask.sum() < 20:
            print(f"  >= {t:4.1f}x  {base*100:9.2f}%  {ev_all:+10.4f}  {'(no samples)':>11}")
            continue
        sel_rate = float(y[mask].mean())
        ev_sel   = sel_rate * (t - 1) - (1 - sel_rate)
        cov      = float(mask.mean())
        print(f"  >= {t:4.1f}x  {base*100:9.2f}%  {ev_all:+10.4f}  "
              f"{ev_sel:+11.4f}  {cov*100:8.1f}%  {ev_sel - ev_all:+9.4f}")

    print("-" * W)
    print("  EV = P(win)*(cashout-1) - P(loss)*1   (per unit bet)")
    print("  Cutoff = 5% above base rate (adaptive)")

    # ── Feature importance (final model proxy) ────────────────────────────────
    print("\n" + "-" * W)
    print("TOP FEATURES BY THRESHOLD (from last CV fold — indicative only)")
    try:
        import xgboost as xgb

        train_crashes = crashes[: int(len(crashes) * 0.8)]
        X_tr, y_tr_d, feat_names = build_feature_matrix(train_crashes)

        for t in [7.0, 8.0, 2.5]:
            y_tr = y_tr_d[t]
            pr   = float(y_tr.mean())
            if pr in (0.0, 1.0):
                continue
            spw = (1.0 - pr) / max(pr, 1e-9)
            xm  = xgb.XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
                scale_pos_weight=spw, eval_metric="logloss",
                tree_method="hist", random_state=42, n_jobs=1, verbosity=0,
            )
            xm.fit(X_tr, y_tr)
            imp = xm.feature_importances_
            top5_idx = np.argsort(imp)[::-1][:5]
            top5 = [(feat_names[i], imp[i]) for i in top5_idx]
            print(f"\n  >= {t:.1f}x  top features:")
            for name, score in top5:
                bar = "█" * int(score * 200)
                print(f"    {name:<25} {score:.4f}  {bar}")
    except Exception as exc:
        print(f"  (feature importance unavailable: {exc})")

    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save",  action="store_true", help="Save final model to models/ensemble.pkl")
    ap.add_argument("--folds", type=int, default=5,  help="CV folds (default 5)")
    ap.add_argument("--quiet", action="store_true",  help="Suppress fold-level output")
    args = ap.parse_args()

    print("Loading crash data …")
    crashes = load_crashes()
    print(f"  {len(crashes):,} rounds loaded")

    print(f"\nRunning {args.folds}-fold walk-forward CV "
          f"({len(crashes) // args.folds:,} rounds/fold) …")
    fold_metrics, oof_y, oof_p = walk_forward_cv(
        crashes, n_folds=args.folds, verbose=not args.quiet
    )

    print_report(crashes, fold_metrics, oof_y, oof_p)

    if args.save:
        print("\nTraining final model on all data …")
        pred = CrashPredictor()
        pred.fit(crashes, verbose=not args.quiet)
        out = pred.save()
        print(f"Saved → {out}")

        print("\nSanity check — predict from last 20 crashes:")
        probs = pred.predict_proba(crashes[-20:].tolist())
        for t, p in sorted(probs.items()):
            bar = "█" * int(p * 40)
            print(f"  P(>={t:4.1f}x) = {p:.4f}  {bar}")


if __name__ == "__main__":
    main()
