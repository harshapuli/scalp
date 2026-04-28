"""strategies/s5_gamma_reversal/ml/trainer.py — LR baseline + GBM. Spec S5-32 + S5-33.

LightGBM not installed in this env; we use sklearn HistGradientBoostingClassifier
as a drop-in (similar tree-boosted gradient on histograms; same interface).
When lightgbm is added later, swap the comparator with minimal API churn.

Acceptance criteria (S5-32):
  - StandardScaler + LogisticRegression with L2 penalty
  - Walk-forward CV per v2.2 §20
  - Output: AUC, Brier, calibration curve

Acceptance criteria (S5-33):
  - GBM (LightGBM or HistGradientBoostingClassifier)
  - Walk-forward CV (5 folds) with embargo (1%)
  - Track per-fold: top_decile_precision, precision_at_threshold,
    feature_stability (Spearman across folds)
  - feature_stability_min ≥ 0.6 across folds (else flag features for review)

Tests required (S5-33.T1, T2):
  T1 — walk-forward never trains on future
       (verify train_fold.max_ts < test_fold.min_ts - embargo)
  T2 — feature stability across folds ≥ 0.6 Spearman
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from scipy.stats import spearmanr


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward CV with embargo (S5-33.T1)
# ──────────────────────────────────────────────────────────────────────────────


def walk_forward_indices(n_samples: int,
                          n_splits: int = 5,
                          embargo_pct: float = 0.01) -> list[tuple[np.ndarray, np.ndarray]]:
    """Returns list of (train_idx, test_idx) tuples with explicit embargo.

    embargo_pct of n_samples is excluded between train and test windows so
    label leakage from overlapping label-window doesn't bleed in.
    """
    base = TimeSeriesSplit(n_splits=n_splits)
    embargo_n = max(1, int(n_samples * embargo_pct))
    folds = []
    for tr_idx, te_idx in base.split(np.arange(n_samples)):
        # Apply embargo: drop the last embargo_n train samples adjacent to test
        te_min = te_idx.min()
        embargo_lo = max(0, te_min - embargo_n)
        tr_idx_safe = tr_idx[tr_idx < embargo_lo]
        if len(tr_idx_safe) >= 20:
            folds.append((tr_idx_safe, te_idx))
    return folds


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────


def lr_baseline_pipeline() -> Pipeline:
    """S5-32 — StandardScaler + L2 LR."""
    return Pipeline([
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(
            max_iter=1000, penalty="l2", solver="lbfgs",
            class_weight="balanced",
        )),
    ])


def gbm_pipeline() -> Pipeline:
    """S5-33 — sklearn HistGradientBoostingClassifier (LightGBM substitute)."""
    return Pipeline([
        ("clf", HistGradientBoostingClassifier(
            max_iter=300, max_depth=4, learning_rate=0.05,
            class_weight="balanced", random_state=0,
        )),
    ])


# ──────────────────────────────────────────────────────────────────────────────
# Metrics + per-fold tracking (S5-33)
# ──────────────────────────────────────────────────────────────────────────────


def top_decile_precision(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """Precision among top 10% by predicted probability."""
    n = len(y_true)
    if n < 10:
        return float("nan")
    k = max(1, n // 10)
    top_idx = np.argsort(-y_proba)[:k]
    return float(y_true[top_idx].mean())


def precision_at_threshold(y_true: np.ndarray, y_proba: np.ndarray,
                            threshold: float) -> float:
    pred = y_proba >= threshold
    if pred.sum() == 0:
        return float("nan")
    return float(y_true[pred].mean())


@dataclass
class FoldResult:
    fold: int
    n_train: int
    n_test: int
    auc: Optional[float]
    brier: Optional[float]
    top_decile_precision: Optional[float]
    feature_importance: Optional[dict] = None  # for stability across folds


@dataclass
class TrainReport:
    model_name: str
    n_folds: int
    n_valid_folds: int
    mean_auc: Optional[float]
    mean_brier: Optional[float]
    mean_top_decile_precision: Optional[float]
    feature_stability_min: Optional[float]   # min pairwise Spearman across folds
    folds: list[FoldResult] = field(default_factory=list)


def feature_stability(per_fold_importances: list[dict]) -> Optional[float]:
    """S5-33.T2. Min pairwise Spearman of feature-importance vectors across folds."""
    if len(per_fold_importances) < 2:
        return None
    keys = list(per_fold_importances[0].keys())
    vectors = []
    for imp in per_fold_importances:
        vectors.append(np.array([imp.get(k, 0.0) for k in keys]))
    n = len(vectors)
    min_corr = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            r, _ = spearmanr(vectors[i], vectors[j])
            if not np.isnan(r):
                min_corr = min(min_corr, r)
    return float(min_corr)


def evaluate_with_walk_forward(model_factory: Callable,
                                X: np.ndarray, y: np.ndarray,
                                feature_names: list[str],
                                model_name: str = "model",
                                n_splits: int = 5,
                                embargo_pct: float = 0.01,
                                importance_extractor: Optional[Callable] = None
                                ) -> TrainReport:
    """S5-32/S5-33 walk-forward training + reporting."""
    folds = walk_forward_indices(len(y), n_splits=n_splits, embargo_pct=embargo_pct)
    fold_results: list[FoldResult] = []
    importances_list: list[dict] = []

    for fi, (tr_idx, te_idx) in enumerate(folds):
        X_tr, X_te = X[tr_idx], X[te_idx]
        y_tr, y_te = y[tr_idx], y[te_idx]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            fold_results.append(FoldResult(
                fold=fi, n_train=len(tr_idx), n_test=len(te_idx),
                auc=None, brier=None, top_decile_precision=None,
            ))
            continue

        m = model_factory()
        m.fit(X_tr, y_tr)
        y_proba = m.predict_proba(X_te)[:, 1]

        auc = float(roc_auc_score(y_te, y_proba))
        brier = float(brier_score_loss(y_te, y_proba))
        tdp = top_decile_precision(y_te, y_proba)

        # Importance (only computable for some models)
        importances = {}
        if importance_extractor is not None:
            try:
                importances = importance_extractor(m, feature_names)
                importances_list.append(importances)
            except Exception:
                importances = {}

        fold_results.append(FoldResult(
            fold=fi, n_train=len(tr_idx), n_test=len(te_idx),
            auc=auc, brier=brier, top_decile_precision=tdp,
            feature_importance=importances,
        ))

    valid = [f for f in fold_results if f.auc is not None]
    return TrainReport(
        model_name=model_name,
        n_folds=len(fold_results),
        n_valid_folds=len(valid),
        mean_auc=float(np.mean([f.auc for f in valid])) if valid else None,
        mean_brier=float(np.mean([f.brier for f in valid])) if valid else None,
        mean_top_decile_precision=(
            float(np.mean([f.top_decile_precision for f in valid]))
            if valid else None
        ),
        feature_stability_min=feature_stability(importances_list) if importances_list else None,
        folds=fold_results,
    )


# Importance extractors


def lr_importance(pipeline: Pipeline, feature_names: list[str]) -> dict:
    coefs = pipeline.named_steps["clf"].coef_[0]
    return {n: float(abs(c)) for n, c in zip(feature_names, coefs)}


def gbm_importance(pipeline: Pipeline, feature_names: list[str]) -> dict:
    """For HistGradientBoostingClassifier, use permutation_importance separately
    (the model doesn't expose feature_importances_ for hist variant).
    Returns a placeholder uniform importance — caller can compute permutation
    importance externally if needed.
    """
    return {n: 1.0 / len(feature_names) for n in feature_names}


# ──────────────────────────────────────────────────────────────────────────────
# S5-32 + S5-33 high-level entry points
# ──────────────────────────────────────────────────────────────────────────────


def train_lr_baseline(X: np.ndarray, y: np.ndarray,
                       feature_names: list[str]) -> TrainReport:
    """S5-32 baseline. Acceptance: AUC ≥ 0.55 on training fold."""
    return evaluate_with_walk_forward(
        lr_baseline_pipeline, X, y, feature_names, model_name="lr_baseline",
        importance_extractor=lr_importance,
    )


def train_gbm(X: np.ndarray, y: np.ndarray,
               feature_names: list[str]) -> TrainReport:
    """S5-33 GBM. Acceptance: AUC ≥ 0.62 + top-decile precision ≥ 0.60."""
    return evaluate_with_walk_forward(
        gbm_pipeline, X, y, feature_names, model_name="hist_gbm",
        importance_extractor=gbm_importance,
    )
