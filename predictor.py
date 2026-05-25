"""
Ensemble crash-multiplier predictor for Aviator bot.

XGBoost + HistGradientBoostingClassifier (sklearn) soft-voting ensemble.
Predicts P(next crash >= threshold) for several cashout thresholds.

Usage
-----
Training (standalone):
    python train_predictor.py

Inference (from bot loop):
    pred = predictor.predict_proba(crash_history[-20:])
    p_7x = pred[7.0]   # e.g. 0.18 — 18% chance next crash >= 7x
"""

import csv
import glob
import logging
import os
import pickle
import threading
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

THRESHOLDS = [2.0, 2.5, 3.0, 3.5, 5.0, 7.0, 8.0, 10.0]
LAG_N      = 15      # number of prior crashes used as features
HISTORY_DIR = Path(__file__).parent / "history"
MODEL_PATH  = Path(__file__).parent / "models" / "ensemble.pkl"

# ── Feature engineering ───────────────────────────────────────────────────────

def _make_feature_row(arr: np.ndarray) -> Dict[str, float]:
    """
    Build one feature row from exactly LAG_N crash values.
    `arr[0]` is the oldest, `arr[-1]` is the most recent prior crash.
    """
    row: Dict[str, float] = {}

    # Lag features (raw + log)
    for j in range(1, LAG_N + 1):
        v = arr[LAG_N - j]
        row[f"lag_{j}"]     = float(v)
        row[f"log_lag_{j}"] = float(np.log(max(v, 1e-6)))

    # Rolling statistics over windows
    for w in (3, 5, 10, LAG_N):
        sub = arr[LAG_N - w:]
        row[f"roll_mean_{w}"]     = float(sub.mean())
        row[f"roll_std_{w}"]      = float(sub.std() if w > 1 else 0.0)
        row[f"roll_max_{w}"]      = float(sub.max())
        row[f"roll_min_{w}"]      = float(sub.min())
        row[f"log_roll_mean_{w}"] = float(np.log(max(sub.mean(), 1e-6)))

    # Count of crashes above key thresholds in the full window
    for t in (1.5, 2.0, 2.5, 5.0, 10.0):
        row[f"cnt_above_{t}"] = int((arr >= t).sum())

    # Consecutive low-crash streak (how many most-recent are below threshold)
    for threshold, label in ((2.0, "2"), (3.5, "3p5")):
        streak = 0
        for j in range(1, LAG_N + 1):
            if arr[LAG_N - j] < threshold:
                streak += 1
            else:
                break
        row[f"streak_below_{label}"] = streak

    # Rounds since last high crash
    for high in (5.0, 8.0, 10.0):
        rounds_since = LAG_N  # default: not seen in window
        for j in range(1, LAG_N + 1):
            if arr[LAG_N - j] >= high:
                rounds_since = j
                break
        row[f"since_{high}x"] = rounds_since

    return row


def build_feature_matrix(crashes: np.ndarray):
    """
    Build (X, y_dict, feature_names) from a flat array of crash multipliers.
    X shape: (N - LAG_N, n_features)
    y_dict: {threshold: binary_array}
    """
    n = len(crashes)
    rows = [_make_feature_row(crashes[i - LAG_N : i]) for i in range(LAG_N, n)]
    feature_names = list(rows[0].keys())
    X = np.array([[r[k] for k in feature_names] for r in rows], dtype=np.float32)
    targets = crashes[LAG_N:]
    y_dict = {t: (targets >= t).astype(np.int8) for t in THRESHOLDS}
    return X, y_dict, feature_names


# ── Data loading ──────────────────────────────────────────────────────────────

def load_crashes(history_dir: Optional[Path] = None) -> np.ndarray:
    """Load crash_mult from all CSVs in history_dir, sorted by filename."""
    directory = history_dir or HISTORY_DIR
    files = sorted(glob.glob(str(directory / "*.csv")))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {directory}")

    all_crashes: List[float] = []
    for path in files:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    all_crashes.append(float(row["crash_mult"]))
                except (KeyError, ValueError):
                    pass

    return np.array(all_crashes, dtype=np.float64)


# ── Model ─────────────────────────────────────────────────────────────────────

class CrashPredictor:
    """
    Soft-voting ensemble:
      Model A — XGBClassifier (xgboost)
      Model B — HistGradientBoostingClassifier (sklearn, LightGBM-style)

    One binary classifier per threshold in THRESHOLDS.
    """

    def __init__(self, thresholds: Optional[List[float]] = None):
        self.thresholds: List[float] = thresholds or THRESHOLDS
        self._xgb_models:  Dict[float, object] = {}
        self._hgb_models:  Dict[float, object] = {}
        self._feature_names: List[str] = []
        self._trained = False

    # ── Training ──────────────────────────────────────────────────────────────

    def fit(self, crashes: np.ndarray, verbose: bool = True) -> "CrashPredictor":
        """Train on a flat crash-multiplier array."""
        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError("pip install xgboost")
        from sklearn.ensemble import HistGradientBoostingClassifier

        X, y_dict, feat_names = build_feature_matrix(crashes)
        self._feature_names = feat_names

        for threshold in self.thresholds:
            y = y_dict[threshold]
            pos_rate = float(y.mean())
            if verbose:
                print(f"  [{threshold:4.1f}x]  positives={y.sum():5d}/{len(y)}  "
                      f"base_rate={pos_rate:.4f}", flush=True)

            spw = (1.0 - pos_rate) / max(pos_rate, 1e-9)

            # XGBoost
            xgb_m = xgb.XGBClassifier(
                n_estimators=200,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.7,
                min_child_weight=10,
                scale_pos_weight=spw,
                eval_metric="logloss",
                tree_method="hist",     # fast histogram splits (required for speed)
                random_state=42,
                n_jobs=1,               # single thread — safe inside background daemon
                verbosity=0,
            )
            xgb_m.fit(X, y)
            self._xgb_models[threshold] = xgb_m

            # HistGradientBoosting (sklearn LightGBM-style, no extra install)
            hgb_m = HistGradientBoostingClassifier(
                max_iter=200,
                max_depth=4,
                learning_rate=0.05,
                min_samples_leaf=10,
                class_weight={0: 1.0, 1: spw},
                random_state=42,
            )
            hgb_m.fit(X, y)
            self._hgb_models[threshold] = hgb_m

        self._trained = True
        return self

    # ── Inference ─────────────────────────────────────────────────────────────

    def predict_proba(self, recent_crashes: List[float]) -> Dict[float, float]:
        """
        Given recent crash history (most-recent last), return
        {threshold: P(next_crash >= threshold)} for each trained threshold.
        Requires at least 1 value; pads with 2.0 if fewer than LAG_N.
        """
        if not self._trained:
            raise RuntimeError("Model not trained. Call .fit() or .load().")

        arr = np.array(recent_crashes, dtype=np.float64)
        if len(arr) >= LAG_N:
            arr = arr[-LAG_N:]
        else:
            arr = np.concatenate([np.full(LAG_N - len(arr), 2.0), arr])

        row = _make_feature_row(arr)
        X = np.array([[row[k] for k in self._feature_names]], dtype=np.float32)

        result: Dict[float, float] = {}
        for t in self.thresholds:
            p_xgb = float(self._xgb_models[t].predict_proba(X)[0, 1])
            p_hgb = float(self._hgb_models[t].predict_proba(X)[0, 1])
            result[t] = (p_xgb + p_hgb) / 2.0
        return result

    def predict_proba_batch(self, X: np.ndarray) -> Dict[float, np.ndarray]:
        """Batch prediction on a pre-built feature matrix."""
        return {
            t: (np.array(self._xgb_models[t].predict_proba(X)[:, 1]) +
                np.array(self._hgb_models[t].predict_proba(X)[:, 1])) / 2.0
            for t in self.thresholds
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Optional[Path] = None) -> Path:
        out = Path(path) if path else MODEL_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fh:
            pickle.dump(self, fh)
        log.info("Predictor saved → %s", out)
        return out

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CrashPredictor":
        inp = Path(path) if path else MODEL_PATH
        with open(inp, "rb") as fh:
            obj = pickle.load(fh)
        log.info("Predictor loaded ← %s", inp)
        return obj


# ── Auto-retraining wrapper ───────────────────────────────────────────────────

class AutoRetrainPredictor:
    """
    Wraps CrashPredictor with background auto-retraining.

    Call update(crash_mult) after every round.  Once min_train_rounds
    samples are available, a CrashPredictor is trained in a daemon thread.
    After that it retrains every retrain_every new rounds, always on a
    snapshot of the full accumulated history.

    Thread-safe: predict/update can be called from any thread.
    """

    def __init__(
        self,
        retrain_every: int = 500,
        min_train_rounds: int = 1000,
        initial_data: Optional[List[float]] = None,
    ):
        self._predictor: Optional[CrashPredictor] = None
        self._lock = threading.Lock()
        self._buffer: List[float] = list(initial_data) if initial_data else []
        self._rounds_since_train: int = retrain_every   # so first train fires quickly
        self._is_training: bool = False
        self.retrain_every = retrain_every
        self.min_train_rounds = min_train_rounds

        if len(self._buffer) >= self.min_train_rounds:
            self._trigger_retrain()

    # ── Feed ─────────────────────────────────────────────────────────────────

    def update(self, crash_mult: float) -> None:
        """Append one round result.  Triggers background retrain when due."""
        self._buffer.append(crash_mult)
        self._rounds_since_train += 1
        if (len(self._buffer) >= self.min_train_rounds
                and self._rounds_since_train >= self.retrain_every
                and not self._is_training):
            self._trigger_retrain()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _trigger_retrain(self) -> None:
        self._is_training = True
        data = np.array(self._buffer)           # copy before handing off
        t = threading.Thread(target=self._worker, args=(data,), daemon=True)
        t.start()

    def _worker(self, data: np.ndarray) -> None:
        import warnings
        warnings.filterwarnings("ignore")
        try:
            m = CrashPredictor()
            m.fit(data, verbose=False)
            with self._lock:
                self._predictor = m
                self._rounds_since_train = 0
            log.info("Predictor: retrained on %d rounds.", len(data))
        except Exception as exc:
            log.error("Predictor: retrain failed — %s", exc)
        finally:
            self._is_training = False

    # ── Inference ────────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """True once the first training cycle has completed."""
        with self._lock:
            return self._predictor is not None

    def get_prob_at(self, cashout: float, recent_crashes: List[float]) -> float:
        """
        Return P(next crash >= cashout).
        Picks the nearest trained threshold when cashout isn't an exact match.
        Returns -1.0 if the model is not yet trained.
        """
        with self._lock:
            pred = self._predictor
        if pred is None:
            return -1.0
        probs = pred.predict_proba(recent_crashes)
        if cashout in probs:
            return probs[cashout]
        nearest = min(probs.keys(), key=lambda t: abs(t - cashout))
        return probs[nearest]
