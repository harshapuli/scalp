"""
strategies/s5_gamma_reversal/ml/trainer.py — LR baseline + LightGBM. Spec S5-32 + S5-33.

Acceptance criteria:
  S5-32 LR baseline:
    - StandardScaler + LogisticRegression with L2 penalty
    - Walk-forward CV per v2.2 §20
    - Output: AUC, Brier, calibration curve
    - Pass: AUC ≥ 0.55 on training (else features insufficient)

  S5-33 LightGBM:
    - LightGBM with early stopping
    - Optuna 50 trials, log all
    - Walk-forward CV (5 folds) with embargo (1%)
    - Track per-fold: top_decile_precision, precision_at_threshold,
      feature_stability (Spearman across folds)
    - feature_stability_min ≥ 0.6 across folds (else flag features for review)
    - Output: AUC, Brier, top-decile precision, feature importance, SHAP

Tests required (S5-33.T1, T2):
  T1 — walk-forward never trains on future
       (verify train_fold.max_ts < test_fold.min_ts - embargo)
  T2 — feature stability across folds ≥ 0.6 Spearman

TODO Sprint 12.
"""
from __future__ import annotations


def train_lr_baseline(X, y, ts_index, cfg: dict):
    """S5-32. TODO Sprint 12."""
    raise NotImplementedError("S5-32")


def train_lightgbm_with_optuna(X, y, ts_index, cfg: dict, n_trials: int = 50):
    """S5-33 + sub-tasks .1 .2 .3. TODO Sprint 12."""
    raise NotImplementedError("S5-33")
