"""
pretrade_slow_predictor.py — does the slow-regime label have ANY pre-trade signal?

The 7y archive replay showed P(T1+|fast) − P(T1+|slow) = +49.6pp, with 100% of
slow fires already STOP'd by the time the 30-bar regime window completes
(median 1 bar to STOP). So the regime classifier itself can't be used pre-trade.

This module asks the deeper question: can we predict the slow label from
features observable at signal time? If yes, we have a real production gate.
If no, the slow regime is essentially irreducible noise from a pre-trade view
and the regime tag stays a post-trade descriptor only.

Target: y = (regime == "slow")  — the durable, validated label, not the bar-5
  stop event. If the stop level changes in the future, the regime label is
  invariant; the stop event isn't.

Features (pre-trade only — strict no-leakage from forward bars):
  - atr_14_pct                 — ATR(14)% computed from prior 14 1m bars
  - minute_of_day              — minutes since NYSE open (negative = pre-market)
  - prior_5bar_return_pct      — close-to-close return on prior 5 1m bars
  - prior_30bar_realized_vol_pct — stdev of returns over prior 30 1m bars
  - prior_5bar_alignment       — signed prior_5bar return × signal_side
                                (+ = with-trend, − = counter-trend)
  - prior_5bar_volume_z        — last 5 bar mean volume / 30-bar mean volume
  - is_call                    — 1 if CALL, 0 if PUT

Model:
  - LogisticRegression baseline (interpretable; coefficient signs = directional)
  - HistGradientBoostingClassifier comparator (captures non-linearity)

Validation:
  - 5-fold TimeSeriesSplit (sorted by date, no random shuffles)
  - Aggregate AUC, top-decile precision, per-fold breakdown
  - Explicit year-holdout summary (train ≤2024, val 2025, test 2026)
  - LR coefficients + GBM permutation importance

Acceptance thresholds:
  - AUC ≥ 0.70 → real pre-trade filter, build the production gate
  - 0.60 ≤ AUC < 0.70 → directional but weak; use as ML feature, not gate
  - AUC < 0.60 → no pre-trade signal; slow regime is irreducible noise
"""
from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEVEN_YEAR_DIR = PROJECT_ROOT / "data" / "seven_year_backtest"
OUT_DIR = PROJECT_ROOT / "data" / "pretrade_slow_predictor"

FEATURE_KEYS = [
    "atr_14_pct",
    "minute_of_day",
    "prior_5bar_return_pct",
    "prior_30bar_realized_vol_pct",
    "prior_5bar_alignment",
    "prior_5bar_volume_z",
    "is_call",
]

# Acceptance bands per discussion
AUC_REAL_FILTER = 0.70
AUC_DIRECTIONAL = 0.60


# ──────────────────────────────────────────────────────────────────────────────
# Feature table construction
# ──────────────────────────────────────────────────────────────────────────────


def _load_latest_run() -> dict:
    runs = sorted(SEVEN_YEAR_DIR.glob("run_*.json"))
    if not runs:
        raise FileNotFoundError(
            f"No run_*.json in {SEVEN_YEAR_DIR}. "
            "Run `python3 src/seven_year_backtest.py` first."
        )
    return json.loads(runs[-1].read_text())


def _row_to_features(row: dict) -> Optional[dict]:
    """Extract a single training row from a per_signal entry. None if not eligible."""
    regime = row.get("regime")
    if regime not in ("fast", "slow"):
        return None
    side = row.get("side")
    if side not in ("CALL", "PUT"):
        return None
    return {
        "date": row.get("date"),
        "ticker": row.get("ticker"),
        "y": 1 if regime == "slow" else 0,
        # features
        "atr_14_pct": row.get("atr_14_pct"),
        "minute_of_day": row.get("minute_of_day"),
        "prior_5bar_return_pct": row.get("prior_5bar_return_pct"),
        "prior_30bar_realized_vol_pct": row.get("prior_30bar_realized_vol_pct"),
        "prior_5bar_alignment": row.get("prior_5bar_alignment"),
        "prior_5bar_volume_z": row.get("prior_5bar_volume_z"),
        "is_call": 1 if side == "CALL" else 0,
    }


def _build_dataset(per_signal: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str], dict]:
    """
    Returns:
      X — (n, n_features) imputed feature matrix (median for missing)
      y — (n,) binary target
      dates — (n,) sorted-with-X dates for chronological splitting
      meta — dict with column names, missing-rate per feature, etc.
    """
    rows = [r for r in (_row_to_features(r) for r in per_signal) if r is not None]
    if not rows:
        raise ValueError("No eligible rows for training (need regime in {fast,slow}).")

    # Sort by date so TimeSeriesSplit gets chronological folds
    rows.sort(key=lambda r: r["date"] or "0000")

    # Build raw matrix and track missingness
    n = len(rows)
    raw = np.full((n, len(FEATURE_KEYS)), np.nan, dtype=float)
    missing_per_feat = Counter()
    for i, r in enumerate(rows):
        for j, k in enumerate(FEATURE_KEYS):
            v = r.get(k)
            if v is None:
                missing_per_feat[k] += 1
            else:
                raw[i, j] = float(v)

    # Median impute
    medians = {}
    for j, k in enumerate(FEATURE_KEYS):
        col = raw[:, j]
        valid = col[~np.isnan(col)]
        med = float(np.median(valid)) if len(valid) > 0 else 0.0
        medians[k] = med
        raw[np.isnan(raw[:, j]), j] = med

    y = np.array([r["y"] for r in rows], dtype=int)
    dates = [r["date"] for r in rows]

    meta = {
        "n_total": n,
        "n_slow": int(y.sum()),
        "n_fast": int((1 - y).sum()),
        "base_rate_slow": float(y.mean()),
        "missing_per_feature": {k: missing_per_feat[k] for k in FEATURE_KEYS},
        "median_imputed_values": medians,
        "feature_keys": FEATURE_KEYS,
    }
    return raw, y, dates, meta


# ──────────────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────────────


def _top_decile_precision(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """Precision among top 10% by predicted probability. Decision-relevant for gating."""
    n = len(y_true)
    if n < 10:
        return float("nan")
    k = max(1, n // 10)
    idx = np.argsort(-y_proba)[:k]   # top-k by descending proba
    return float(y_true[idx].mean())


def _summarize_fold(y_true: np.ndarray, y_proba: np.ndarray) -> dict:
    if len(y_true) == 0 or len(np.unique(y_true)) < 2:
        return {"auc": None, "ap": None, "top_decile_precision": None, "n": int(len(y_true))}
    return {
        "auc": float(roc_auc_score(y_true, y_proba)),
        "ap": float(average_precision_score(y_true, y_proba)),
        "top_decile_precision": _top_decile_precision(y_true, y_proba),
        "n": int(len(y_true)),
        "base_rate": float(y_true.mean()),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────


def _lr_pipeline() -> Pipeline:
    return Pipeline([
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced", solver="lbfgs")),
    ])


def _gbm_pipeline() -> Pipeline:
    return Pipeline([
        ("clf", HistGradientBoostingClassifier(
            max_iter=200, max_depth=4, learning_rate=0.05,
            class_weight="balanced", random_state=0,
        )),
    ])


# ──────────────────────────────────────────────────────────────────────────────
# Main: fit + evaluate
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_model(X: np.ndarray, y: np.ndarray, dates: list[str],
                   model_factory, name: str, n_splits: int = 5) -> dict:
    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_results = []
    for fi, (tr_idx, te_idx) in enumerate(tscv.split(X)):
        X_tr, X_te = X[tr_idx], X[te_idx]
        y_tr, y_te = y[tr_idx], y[te_idx]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            fold_results.append({"fold": fi, "skipped": True, "reason": "single-class fold"})
            continue
        model = model_factory()
        model.fit(X_tr, y_tr)
        y_proba = model.predict_proba(X_te)[:, 1]
        fold_summary = _summarize_fold(y_te, y_proba)
        fold_summary["fold"] = fi
        first_te_date = dates[te_idx[0]] if len(te_idx) else None
        last_te_date = dates[te_idx[-1]] if len(te_idx) else None
        fold_summary["test_date_range"] = [first_te_date, last_te_date]
        fold_results.append(fold_summary)

    valid = [f for f in fold_results if f.get("auc") is not None]
    mean_auc = (sum(f["auc"] for f in valid) / len(valid)) if valid else None
    mean_top_dec = (sum(f["top_decile_precision"] for f in valid) / len(valid)
                    if valid and all(f["top_decile_precision"] is not None for f in valid)
                    else None)

    return {
        "name": name,
        "n_folds": len(fold_results),
        "n_valid_folds": len(valid),
        "mean_auc": mean_auc,
        "mean_top_decile_precision": mean_top_dec,
        "folds": fold_results,
    }


def fit_full_and_extract_importances(X: np.ndarray, y: np.ndarray) -> dict:
    """Fit on the full dataset to extract LR coefficients and GBM importances."""
    out = {}
    # LR
    lr = _lr_pipeline()
    lr.fit(X, y)
    coefs = lr.named_steps["clf"].coef_[0].tolist()
    intercept = float(lr.named_steps["clf"].intercept_[0])
    out["lr"] = {
        "intercept": intercept,
        "coefficients": dict(zip(FEATURE_KEYS, coefs)),
        # Sorted by |coef|
        "ranked_by_abs_coef": sorted(
            [(k, c) for k, c in zip(FEATURE_KEYS, coefs)],
            key=lambda kv: -abs(kv[1]),
        ),
    }

    # GBM with permutation importance (more robust than feature_importances_)
    gbm = _gbm_pipeline()
    gbm.fit(X, y)
    perm = permutation_importance(gbm, X, y, n_repeats=10, random_state=0, n_jobs=1)
    perm_means = perm.importances_mean.tolist()
    perm_stds = perm.importances_std.tolist()
    out["gbm"] = {
        "permutation_importance_mean": dict(zip(FEATURE_KEYS, perm_means)),
        "permutation_importance_std": dict(zip(FEATURE_KEYS, perm_stds)),
        "ranked_by_perm_importance": sorted(
            [(k, m) for k, m in zip(FEATURE_KEYS, perm_means)],
            key=lambda kv: -kv[1],
        ),
    }
    return out


def year_holdout_evaluation(X: np.ndarray, y: np.ndarray, dates: list[str]) -> dict:
    """
    Explicit year-holdout: train ≤2024, validate 2025, test 2026.
    Easier to read than 5-fold rolling for the dashboard.
    """
    def _year_of(d):
        return (d or "0000")[:4]
    years = np.array([_year_of(d) for d in dates])
    train_mask = np.isin(years, ["2019", "2020", "2021", "2022", "2023", "2024"])
    val_mask = (years == "2025")
    test_mask = (years == "2026")

    out = {
        "n_train": int(train_mask.sum()),
        "n_val": int(val_mask.sum()),
        "n_test": int(test_mask.sum()),
        "train_base_rate": float(y[train_mask].mean()) if train_mask.sum() else None,
        "val_base_rate": float(y[val_mask].mean()) if val_mask.sum() else None,
        "test_base_rate": float(y[test_mask].mean()) if test_mask.sum() else None,
    }

    # Need both classes in train AND test for AUC
    if (train_mask.sum() < 20 or val_mask.sum() < 10 or
            len(np.unique(y[train_mask])) < 2 or len(np.unique(y[val_mask])) < 2):
        out["status"] = "insufficient_data"
        return out

    for name, factory in (("lr", _lr_pipeline), ("gbm", _gbm_pipeline)):
        m = factory()
        m.fit(X[train_mask], y[train_mask])
        v_proba = m.predict_proba(X[val_mask])[:, 1]
        v = _summarize_fold(y[val_mask], v_proba)
        out[f"{name}_val"] = v
        if test_mask.sum() >= 10 and len(np.unique(y[test_mask])) >= 2:
            t_proba = m.predict_proba(X[test_mask])[:, 1]
            out[f"{name}_test"] = _summarize_fold(y[test_mask], t_proba)
    out["status"] = "ok"
    return out


def verdict_for_auc(auc: Optional[float]) -> str:
    if auc is None:
        return "no data"
    if auc >= AUC_REAL_FILTER:
        return "real pre-trade filter — build production gate"
    if auc >= AUC_DIRECTIONAL:
        return "directional but weak — use as ML feature, not gate"
    # Avoid "irreducible noise" framing — that overclaims. AUC 0.58 means
    # "this 7-feature set doesn't get there." A bigger model with richer features
    # (UW flow, news LLM score, multi-timeframe context, GEX proximity) might
    # push it higher. We've proven feature-set insufficiency, not irreducibility.
    return "below 0.60 with this feature set — reopens with richer features (UW flow, news, GEX)"


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────


def run(write_json: bool = True) -> dict:
    t0 = time.time()
    print("[predictor] loading 7y backtest run...")
    run_data = _load_latest_run()
    per_signal = run_data.get("per_signal", [])
    print(f"[predictor] loaded {len(per_signal)} per_signal rows")

    X, y, dates, meta = _build_dataset(per_signal)
    print(f"[predictor] dataset: n={meta['n_total']}  slow={meta['n_slow']} ({100*meta['base_rate_slow']:.1f}%)  fast={meta['n_fast']}")
    miss = {k: v for k, v in meta["missing_per_feature"].items() if v > 0}
    if miss:
        print(f"[predictor] missing values (median-imputed): {miss}")

    # 5-fold time-series CV
    print("[predictor] running 5-fold TimeSeriesSplit ...")
    lr_cv = evaluate_model(X, y, dates, _lr_pipeline, "logistic_regression", n_splits=5)
    gbm_cv = evaluate_model(X, y, dates, _gbm_pipeline, "histgbm", n_splits=5)
    print(f"[predictor]   LR  mean AUC = {lr_cv['mean_auc']}")
    print(f"[predictor]   GBM mean AUC = {gbm_cv['mean_auc']}")

    # Explicit year holdout (train ≤2024, val 2025, test 2026)
    print("[predictor] running year-holdout evaluation ...")
    yh = year_holdout_evaluation(X, y, dates)
    print(f"[predictor]   train n={yh.get('n_train')}  val n={yh.get('n_val')}  test n={yh.get('n_test')}")
    if yh.get("status") == "ok":
        for kk in ("lr_val", "lr_test", "gbm_val", "gbm_test"):
            v = yh.get(kk)
            if v:
                print(f"[predictor]   {kk}: AUC={v.get('auc')}  top-dec-prec={v.get('top_decile_precision')}")

    # Feature importances (full-data fit)
    print("[predictor] extracting feature importances ...")
    fi = fit_full_and_extract_importances(X, y)
    print("[predictor]   LR top features (by |coef|):")
    for k, c in fi["lr"]["ranked_by_abs_coef"][:5]:
        print(f"[predictor]     {k:35s}  coef={c:+.4f}")
    print("[predictor]   GBM top features (by perm importance):")
    for k, m in fi["gbm"]["ranked_by_perm_importance"][:5]:
        print(f"[predictor]     {k:35s}  imp={m:+.4f}")

    # Headline AUC and verdict — use the better of the two models on the more conservative test (year holdout test set if available, else CV mean)
    headline_auc = None
    headline_source = None
    if yh.get("status") == "ok" and yh.get("gbm_test"):
        headline_auc = yh["gbm_test"]["auc"]
        headline_source = "gbm_test_2026"
    elif yh.get("status") == "ok" and yh.get("gbm_val"):
        headline_auc = yh["gbm_val"]["auc"]
        headline_source = "gbm_val_2025"
    elif gbm_cv.get("mean_auc"):
        headline_auc = gbm_cv["mean_auc"]
        headline_source = "gbm_cv_mean"

    verdict = verdict_for_auc(headline_auc)
    print(f"[predictor] headline AUC = {headline_auc} (source={headline_source}) → {verdict}")

    out = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            "elapsed_sec": time.time() - t0,
            "feature_keys": FEATURE_KEYS,
            "auc_real_filter": AUC_REAL_FILTER,
            "auc_directional": AUC_DIRECTIONAL,
        },
        "dataset": meta,
        "lr_cv": lr_cv,
        "gbm_cv": gbm_cv,
        "year_holdout": yh,
        "feature_importances": fi,
        "headline": {
            "auc": headline_auc,
            "source": headline_source,
            "verdict": verdict,
        },
    }

    if write_json:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        d = datetime.now().strftime("%Y-%m-%d")
        path = OUT_DIR / f"run_{d}.json"
        path.write_text(json.dumps(out, indent=2, default=str))
        print(f"[predictor] wrote {path} ({path.stat().st_size/1024:.1f} KB)")

    return out


if __name__ == "__main__":
    run()
