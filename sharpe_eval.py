#!/usr/bin/env python3
"""
Sharpe-ratio evaluation of XGBoost + CatBoost crash-multiplier models.

Loads ALL CSVs from history/, stacks them chronologically by timestamp,
then runs leave-one-date-out walk-forward CV (train = all prior dates,
test = one date).  Reports per-date and aggregate Sharpe ratios plus
the standard AUC / EV metrics.

Usage:
    source .venv/bin/activate
    python sharpe_eval.py [--thresholds 2.5 3.5 7.0] [--stake 50] [--quiet]
"""

import argparse
import csv
import glob
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))

from predictor import THRESHOLDS, LAG_N, build_feature_matrix

HISTORY_DIR = Path(__file__).parent / "history"

# Thresholds to show in per-date table (subset of THRESHOLDS)
REPORT_THRESHOLDS = [2.5, 3.5, 7.0]


# ── Data loading ───────────────────────────────────────────────────────────────

def load_dated_crashes(history_dir: Path = HISTORY_DIR) -> Tuple[np.ndarray, np.ndarray]:
    """
    Load crash_mult from all CSVs, sorted by timestamp.

    Returns
    -------
    crashes   : float64 ndarray, shape (N,)
    dates     : object  ndarray of 'YYYY-MM-DD' strings, shape (N,)
    """
    records: List[Tuple[str, float, str]] = []   # (iso_ts, crash_mult, date)

    for path in sorted(glob.glob(str(history_dir / "*.csv"))):
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    try:
                        mult = float(row["crash_mult"])
                    except (KeyError, ValueError):
                        continue
                    raw_ts = row.get("timestamp", "")
                    try:
                        # Handle both "2026-05-19 08:01:03" and "5/23/26 8:22"
                        from datetime import datetime
                        for fmt in (
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d %H:%M",
                            "%m/%d/%y %H:%M",
                            "%m/%d/%Y %H:%M",
                        ):
                            try:
                                dt = datetime.strptime(raw_ts.strip(), fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            # fallback: derive date from filename
                            stem = Path(path).stem          # aviator_20260519
                            digits = "".join(c for c in stem if c.isdigit())[:8]
                            dt_str = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
                            records.append((raw_ts, mult, dt_str))
                            continue
                        iso = dt.strftime("%Y-%m-%d %H:%M:%S")
                        date = dt.strftime("%Y-%m-%d")
                        records.append((iso, mult, date))
                    except Exception:
                        continue
        except Exception as exc:
            print(f"  Warning: could not read {path}: {exc}")

    if not records:
        raise FileNotFoundError(f"No usable rows in {history_dir}")

    # Sort chronologically by timestamp string (ISO format sorts lexicographically)
    records.sort(key=lambda r: r[0])

    crashes = np.array([r[1] for r in records], dtype=np.float64)
    dates   = np.array([r[2] for r in records], dtype=object)
    return crashes, dates


# ── Sharpe helpers ─────────────────────────────────────────────────────────────

def sharpe(pnl: np.ndarray) -> float:
    """Per-round Sharpe (unannualised): mean / std.  NaN if fewer than 5 samples."""
    if len(pnl) < 5 or pnl.std() == 0:
        return float("nan")
    return float(pnl.mean() / pnl.std())


def model_pnl(y: np.ndarray, p: np.ndarray,
               threshold: float, stake: float = 1.0,
               cutoff: Optional[float] = None) -> np.ndarray:
    """
    Build a PnL array:  bet when p >= cutoff, else 0.
    Win pays stake*(threshold-1), loss costs -stake.
    """
    if cutoff is None:
        base = y.mean() if len(y) else 0.5
        cutoff = max(base * 1.05, 0.5 * base + 0.5)
    mask = p >= cutoff
    pnl  = np.zeros(len(y), dtype=np.float64)
    wins = mask & (y == 1)
    loss = mask & (y == 0)
    pnl[wins] = stake * (threshold - 1)
    pnl[loss] = -stake
    return pnl


def always_bet_pnl(y: np.ndarray, threshold: float, stake: float = 1.0) -> np.ndarray:
    pnl = np.full(len(y), -stake, dtype=np.float64)
    pnl[y == 1] = stake * (threshold - 1)
    return pnl


# ── Model training ─────────────────────────────────────────────────────────────

def _train_predict(
    X_tr: np.ndarray, y_tr: np.ndarray,
    X_te: np.ndarray,
    spw: float,
    use_catboost: bool = True,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (p_xgb, p_cat, p_ensemble)."""
    import xgboost as xgb

    xm = xgb.XGBClassifier(
        n_estimators=150, max_depth=4, learning_rate=0.06,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
        scale_pos_weight=spw, eval_metric="logloss",
        tree_method="hist", random_state=42,
        n_jobs=2,           # limit threads to avoid thrashing
        verbosity=0,
    )
    xm.fit(X_tr, y_tr)
    p_xgb = np.array(xm.predict_proba(X_te)[:, 1])

    if use_catboost:
        try:
            from catboost import CatBoostClassifier
            cm = CatBoostClassifier(
                iterations=150, depth=5, learning_rate=0.06,
                class_weights=[1.0, spw],
                eval_metric="AUC", random_seed=42,
                thread_count=2,     # cap threads — avoids CPU thrashing
                verbose=0, allow_writing_files=False,
            )
            cm.fit(X_tr, y_tr)
            p_cat = np.array(cm.predict_proba(X_te)[:, 1])
        except Exception:
            from sklearn.ensemble import HistGradientBoostingClassifier as HGBC
            hm = HGBC(max_iter=150, max_depth=4, learning_rate=0.06,
                      min_samples_leaf=10,
                      class_weight={0: 1.0, 1: spw}, random_state=42)
            hm.fit(X_tr, y_tr)
            p_cat = np.array(hm.predict_proba(X_te)[:, 1])
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier as HGBC
        hm = HGBC(max_iter=150, max_depth=4, learning_rate=0.06,
                  min_samples_leaf=10,
                  class_weight={0: 1.0, 1: spw}, random_state=42)
        hm.fit(X_tr, y_tr)
        p_cat = np.array(hm.predict_proba(X_te)[:, 1])

    p_ens = (p_xgb + p_cat) / 2.0
    return p_xgb, p_cat, p_ens


# ── AUC helper ────────────────────────────────────────────────────────────────

def _auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else float("nan")


# ── Walk-forward by date ───────────────────────────────────────────────────────

def run_date_walk_forward(
    crashes: np.ndarray,
    dates: np.ndarray,
    stake: float = 1.0,
    verbose: bool = True,
) -> Dict:
    """
    Leave-one-date-out walk-forward.  For each date D (in order), train on
    all rounds with earlier dates, test on rounds dated D.

    Returns nested dict:
      results[date][threshold] = {
          'n': int, 'base': float, 'auc_xgb': float, 'auc_cat': float,
          'auc_ens': float,
          'sharpe_always': float,
          'sharpe_xgb': float, 'sharpe_cat': float, 'sharpe_ens': float,
          'ev_always': float, 'ev_xgb': float,
      }
    """
    unique_dates = sorted(np.unique(dates))
    if len(unique_dates) < 2:
        raise ValueError("Need at least 2 distinct dates for walk-forward CV.")

    results: Dict = {}

    for i, test_date in enumerate(unique_dates[1:], 1):
        train_mask = dates < test_date
        test_mask  = dates == test_date

        train_crashes = crashes[train_mask]
        test_crashes  = crashes[test_mask]

        if len(train_crashes) < LAG_N * 3 or len(test_crashes) < LAG_N:
            if verbose:
                print(f"  {test_date}: skipped (train={len(train_crashes)}, test={len(test_crashes)})")
            continue

        # Include LAG_N-length overlap from train tail so test features are valid
        seg = np.concatenate([train_crashes[-LAG_N:], test_crashes])
        X_tr, y_tr_d, _ = build_feature_matrix(train_crashes)
        X_sg, y_sg_d, _ = build_feature_matrix(seg)
        X_te   = X_sg[LAG_N:]        # skip overlap rows
        y_te_d = {t: y_sg_d[t][LAG_N:] for t in THRESHOLDS}

        if verbose:
            print(f"  {test_date}  train={len(train_crashes):>5,}  test={len(test_crashes):>4,}",
                  end="", flush=True)

        day_res: Dict = {}

        for t in REPORT_THRESHOLDS:
            y_tr = y_tr_d[t]
            y_te = y_te_d[t]
            pr   = float(y_tr.mean())
            if pr in (0.0, 1.0) or len(y_te) < 5:
                continue
            spw = (1.0 - pr) / max(pr, 1e-9)

            p_xgb, p_cat, p_ens = _train_predict(X_tr, y_tr, X_te, spw)

            base = float(y_te.mean())
            cutoff = max(base * 1.05, 0.5 * base + 0.5)

            pnl_always = always_bet_pnl(y_te, t, stake)
            pnl_xgb    = model_pnl(y_te, p_xgb, t, stake, cutoff)
            pnl_cat    = model_pnl(y_te, p_cat, t, stake, cutoff)
            pnl_ens    = model_pnl(y_te, p_ens, t, stake, cutoff)

            day_res[t] = {
                "n":            len(y_te),
                "base":         base,
                "auc_xgb":      _auc(y_te, p_xgb),
                "auc_cat":      _auc(y_te, p_cat),
                "auc_ens":      _auc(y_te, p_ens),
                "sharpe_always": sharpe(pnl_always),
                "sharpe_xgb":   sharpe(pnl_xgb[pnl_xgb != 0]),
                "sharpe_cat":   sharpe(pnl_cat[pnl_cat != 0]),
                "sharpe_ens":   sharpe(pnl_ens[pnl_ens != 0]),
                "bets_xgb":     int((pnl_xgb != 0).sum()),
                "bets_cat":     int((pnl_cat != 0).sum()),
                "bets_ens":     int((pnl_ens != 0).sum()),
                "pnl_xgb_sum":  float(pnl_xgb.sum()),
                "pnl_cat_sum":  float(pnl_cat.sum()),
                "pnl_ens_sum":  float(pnl_ens.sum()),
                "pnl_always_sum": float(pnl_always.sum()),
            }

        results[test_date] = day_res
        if verbose:
            print("  done")

    return results


# ── Aggregate Sharpe over all OOF rounds ──────────────────────────────────────

def aggregate_oof(
    crashes: np.ndarray,
    dates: np.ndarray,
    stake: float = 1.0,
    verbose: bool = True,
) -> Dict:
    """Single-pass OOF: collect all (y, p) across dates, return aggregate stats."""
    unique_dates = sorted(np.unique(dates))
    oof: Dict = {t: {"y": [], "p_xgb": [], "p_cat": [], "p_ens": []}
                 for t in REPORT_THRESHOLDS}

    for i, test_date in enumerate(unique_dates[1:], 1):
        train_mask = dates < test_date
        test_mask  = dates == test_date
        train_crashes = crashes[train_mask]
        test_crashes  = crashes[test_mask]

        if len(train_crashes) < LAG_N * 3 or len(test_crashes) < LAG_N:
            continue

        seg    = np.concatenate([train_crashes[-LAG_N:], test_crashes])
        X_tr, y_tr_d, _ = build_feature_matrix(train_crashes)
        X_sg, y_sg_d, _ = build_feature_matrix(seg)
        X_te   = X_sg[LAG_N:]
        y_te_d = {t: y_sg_d[t][LAG_N:] for t in THRESHOLDS}

        for t in REPORT_THRESHOLDS:
            y_tr = y_tr_d[t]; y_te = y_te_d[t]
            pr   = float(y_tr.mean())
            if pr in (0.0, 1.0) or len(y_te) < 5:
                continue
            spw = (1.0 - pr) / max(pr, 1e-9)
            p_xgb, p_cat, p_ens = _train_predict(X_tr, y_tr, X_te, spw, use_catboost=True)
            oof[t]["y"].extend(y_te.tolist())
            oof[t]["p_xgb"].extend(p_xgb.tolist())
            oof[t]["p_cat"].extend(p_cat.tolist())
            oof[t]["p_ens"].extend(p_ens.tolist())

    return {t: {k: np.array(v) for k, v in d.items()} for t, d in oof.items()}


# ── Print report ──────────────────────────────────────────────────────────────

def print_report(results: Dict, stake: float, crashes: np.ndarray):
    W = 100
    sign = lambda v: f"{v:+.2f}" if not np.isnan(v) else "   n/a"
    fmt_s = lambda v: f"{v:+.3f}" if not np.isnan(v) else "   n/a"
    fmt_a = lambda v: f"{v:.4f}" if not np.isnan(v) else "  n/a"

    # ── Per-date table ────────────────────────────────────────────────────────
    for t in REPORT_THRESHOLDS:
        print()
        print("=" * W)
        print(f"  THRESHOLD >= {t:.1f}x   (stake={stake:.0f} KES per bet)")
        print("=" * W)
        hdr = (f"  {'Date':<12} {'Rounds':>6}  {'Base%':>5}  "
               f"{'AUC-XGB':>7}  {'AUC-CB':>6}  {'AUC-Ens':>7}  "
               f"{'Sharpe↑Always':>13}  {'Sharpe↑XGB':>10}  "
               f"{'Sharpe↑CAT':>10}  {'Sharpe↑Ens':>10}  "
               f"{'PnL-Ens':>8}  {'Bets':>5}")
        print(hdr)
        print("-" * W)

        totals = defaultdict(float)
        valid_days = 0

        for date in sorted(results.keys()):
            d = results[date].get(t)
            if d is None:
                continue
            valid_days += 1
            for k in ("pnl_always_sum", "pnl_xgb_sum", "pnl_cat_sum", "pnl_ens_sum", "n", "bets_ens"):
                totals[k] += d[k]

            ens_flag = ""
            if not np.isnan(d["sharpe_ens"]) and d["sharpe_ens"] > 0.3:
                ens_flag = " ◄"

            print(f"  {date:<12} {d['n']:>6,}  {d['base']*100:>5.1f}%  "
                  f"{fmt_a(d['auc_xgb']):>7}  {fmt_a(d['auc_cat']):>6}  {fmt_a(d['auc_ens']):>7}  "
                  f"{fmt_s(d['sharpe_always']):>13}  "
                  f"{fmt_s(d['sharpe_xgb']):>10}  "
                  f"{fmt_s(d['sharpe_cat']):>10}  "
                  f"{fmt_s(d['sharpe_ens']):>10}{ens_flag}  "
                  f"{sign(d['pnl_ens_sum']):>8}  {d['bets_ens']:>5}")

        print("-" * W)
        if valid_days:
            print(f"  {'TOTAL':<12} {int(totals['n']):>6,}  {'---':>5}   "
                  f"{'---':>7}   {'---':>6}   {'---':>7}  "
                  f"{'---':>13}  {'---':>10}  {'---':>10}  {'---':>10}  "
                  f"{sign(totals['pnl_ens_sum']):>8}  {int(totals['bets_ens']):>5}")
        print()
        print("  Sharpe = mean(bet_pnl) / std(bet_pnl)  (positive = strategy earns per unit risk)")
        print("  AUC > 0.52 = slight edge   > 0.54 = useful   ◄ Sharpe > 0.30 = noteworthy")


def print_aggregate(oof: Dict, stake: float):
    W = 78
    print()
    print("=" * W)
    print("AGGREGATE OOF SHARPE  (all dates pooled, out-of-fold predictions)")
    print("=" * W)
    sign = lambda v: f"{v:+.3f}" if not np.isnan(v) else "   n/a"

    for t in REPORT_THRESHOLDS:
        d = oof[t]
        if len(d["y"]) == 0:
            continue
        y = d["y"]; p_xgb = d["p_xgb"]; p_cat = d["p_cat"]; p_ens = d["p_ens"]
        base   = float(y.mean())
        cutoff = max(base * 1.05, 0.5 * base + 0.5)

        pnl_always = always_bet_pnl(y, t, stake)
        pnl_xgb    = model_pnl(y, p_xgb, t, stake, cutoff)
        pnl_cat    = model_pnl(y, p_cat, t, stake, cutoff)
        pnl_ens    = model_pnl(y, p_ens, t, stake, cutoff)

        auc_xgb = _auc(y, p_xgb)
        auc_cat = _auc(y, p_cat)
        auc_ens = _auc(y, p_ens)

        cov = float((pnl_ens != 0).mean())

        print(f"\n  >= {t:.1f}x  base={base*100:.1f}%  rounds={len(y):,}  coverage={cov*100:.1f}%")
        print(f"    {'Model':<12}  {'AUC':>7}  {'Sharpe(always)':>14}  {'Sharpe(model)':>13}  "
              f"{'Total PnL':>10}  {'Bets':>5}")
        print(f"    {'-'*12}  {'-'*7}  {'-'*14}  {'-'*13}  {'-'*10}  {'-'*5}")

        for name, p_m, pnl_m in [("XGBoost", p_xgb, pnl_xgb),
                                   ("CatBoost", p_cat, pnl_cat),
                                   ("Ensemble", p_ens, pnl_ens)]:
            auc_v = _auc(y, p_m)
            sh_m  = sharpe(pnl_m[pnl_m != 0])
            sh_a  = sharpe(pnl_always)
            bets  = int((pnl_m != 0).sum())
            flag  = " ◄ EDGE" if (not np.isnan(auc_v) and auc_v > 0.54) else ""
            print(f"    {name:<12}  {auc_v:.4f}  {sign(sh_a):>14}  {sign(sh_m):>13}  "
                  f"{pnl_m.sum():>+10.2f}  {bets:>5}{flag}")

    print()
    print("  Positive Sharpe + AUC>0.52 + Total PnL>0  →  model adds real edge")
    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stake",  type=float, default=50.0,  help="KES per bet (default 50)")
    ap.add_argument("--quiet",  action="store_true",        help="Suppress fold output")
    ap.add_argument("--thresholds", nargs="+", type=float,
                    default=REPORT_THRESHOLDS,              help="Thresholds to evaluate")
    args = ap.parse_args()

    active_thresholds = [t for t in args.thresholds if t in THRESHOLDS]
    if not active_thresholds:
        print("No valid thresholds. Choose from:", THRESHOLDS)
        sys.exit(1)
    REPORT_THRESHOLDS[:] = active_thresholds

    print("Loading crash history (all CSVs, stacked by timestamp)…")
    crashes, dates = load_dated_crashes()
    unique_dates = sorted(np.unique(dates))
    print(f"  {len(crashes):,} rounds  |  {len(unique_dates)} dates: "
          f"{unique_dates[0]} → {unique_dates[-1]}")

    # Per-date breakdown
    print(f"\nDate-walk-forward evaluation (stake={args.stake:.0f} KES)…")
    results = run_date_walk_forward(crashes, dates, stake=args.stake, verbose=not args.quiet)
    print_report(results, args.stake, crashes)

    # Aggregate OOF (same passes but pooled)
    print("\nComputing aggregate OOF predictions…")
    oof = aggregate_oof(crashes, dates, stake=args.stake, verbose=False)
    print_aggregate(oof, args.stake)


if __name__ == "__main__":
    main()
