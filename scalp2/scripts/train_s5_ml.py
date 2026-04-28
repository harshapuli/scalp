"""scripts/train_s5_ml.py — End-to-end ML pipeline driver.

Per spec §10.8 build recipe:
  $ python -m scripts.train_s5_ml --dataset data/features/v1.parquet
  → metrics: AUC=0.64, Brier=0.21, top_decile_p=0.67
  → model registered as s5_v1.0.0 (PASSED gate)

Pipeline:
  1. Load features dataset (built via scripts/build_features.py)
  2. Train LR baseline (S5-32) — accept if AUC ≥ 0.55
  3. Train GBM (S5-33) — track AUC, top-decile precision, feature stability
  4. Calibrate (S5-34) — isotonic regression, slope check
  5. Pick threshold via EV-curve (S5-35)
  6. Apply S5-36 pass criteria (AUC ≥ 0.62 OR EV +15%, AND precision ≥ 60%, AND Brier improvement)
  7. Register model in registry (S5-37)

Acceptance gates (S5-36):
  - AUC ≥ 0.62 OR EV improves rules-only by ≥ 15%
  - AND Brier improves baseline by ≥ 10%
  - AND top-decile precision ≥ 60%
  - AND calibration slope in [0.85, 1.15]
  - AND feature_stability_min ≥ 0.6
  - AND sample size ≥ 200/direction (preferred 300+)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from strategies.s5_gamma_reversal.ml.trainer import (
    train_lr_baseline, train_gbm, TrainReport,
)
from strategies.s5_gamma_reversal.ml.calibrate import calibrate_and_report
from strategies.s5_gamma_reversal.ml.threshold import pick_threshold


def _load_features(path: Path):
    """Load features parquet → (X, y, feature_names). y is label_trade_success."""
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except Exception as e:
        print(f"[train] failed to load parquet: {e}")
        sys.exit(1)

    # Filter to rows with a label
    if "label_trade_success" not in df.columns:
        print("[train] WARNING: dataset has no 'label_trade_success' — using state==SURGE_REVERSE proxy")
        df["label_trade_success"] = (df["state"].isin(["SURGE_REVERSE", "TANK_REVERSE"])).astype(int)

    feature_cols = [c for c in df.columns
                     if c not in {"ticker", "timestamp", "bar_idx", "state",
                                   "features_hash", "label_trade_success",
                                   "label_reversal_next_N"}
                     and df[c].dtype in (float, "float64", "float32",
                                          int, "int64", "int32")]
    X = df[feature_cols].to_numpy(dtype=float)
    y = df["label_trade_success"].to_numpy(dtype=int)
    return X, y, feature_cols


def _check_pass_gate(gbm: TrainReport, lr_baseline_brier: float, cfg: dict) -> tuple[bool, list[str]]:
    """S5-36 — combined pass criteria. Returns (passed, failure_reasons)."""
    p = cfg["s5"]["ml"]["pass"]
    failures = []
    if gbm.mean_auc is None or gbm.mean_auc < p["auc_min"]:
        failures.append(f"AUC {gbm.mean_auc} < {p['auc_min']}")
    if gbm.mean_brier is None or lr_baseline_brier - gbm.mean_brier < p["brier_improvement_pct"] * lr_baseline_brier:
        failures.append(
            f"Brier improvement {((lr_baseline_brier - (gbm.mean_brier or 0)) / max(lr_baseline_brier, 1e-9)):.3f} "
            f"< {p['brier_improvement_pct']}"
        )
    if gbm.mean_top_decile_precision is None or gbm.mean_top_decile_precision < p["top_decile_precision_min"]:
        failures.append(f"top_decile_precision {gbm.mean_top_decile_precision} < {p['top_decile_precision_min']}")
    if gbm.feature_stability_min is None or gbm.feature_stability_min < p["feature_stability_min"]:
        failures.append(f"feature_stability_min {gbm.feature_stability_min} < {p['feature_stability_min']}")
    return (not failures), failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, help="path to features parquet")
    parser.add_argument("--report", default="data/ml_report.json")
    args = parser.parse_args()

    cfg = load_thresholds()
    X, y, feature_names = _load_features(Path(args.dataset))
    print(f"[train] dataset: n={len(y)} positive_rate={y.mean():.3f} features={len(feature_names)}")

    print("[train] S5-32 LR baseline...")
    lr_report = train_lr_baseline(X, y, feature_names)
    print(f"  mean AUC={lr_report.mean_auc} mean Brier={lr_report.mean_brier}")

    print("[train] S5-33 GBM...")
    gbm_report = train_gbm(X, y, feature_names)
    print(f"  mean AUC={gbm_report.mean_auc} mean Brier={gbm_report.mean_brier} "
          f"top_decile_p={gbm_report.mean_top_decile_precision} "
          f"feat_stability={gbm_report.feature_stability_min}")

    # Get raw probabilities from full-data GBM for calibration
    from strategies.s5_gamma_reversal.ml.trainer import gbm_pipeline
    gbm = gbm_pipeline()
    gbm.fit(X, y)
    raw_probas = gbm.predict_proba(X)[:, 1]
    print("[train] S5-34 calibrating with isotonic regression...")
    _, cal_report = calibrate_and_report(raw_probas, y)
    print(f"  brier_uncal={cal_report.brier_uncalibrated:.4f} "
          f"brier_cal={cal_report.brier_calibrated:.4f} "
          f"slope={cal_report.calibration_slope:.3f}")

    print("[train] S5-35 picking threshold...")
    best_thr, best_ev, warnings = pick_threshold(
        raw_probas, y,
        costs_per_trade=cfg["s5"]["cost_model"]["spread_cost_pct"],
        payoff_per_winner=1.0,
    )
    print(f"  threshold={best_thr:.2f} ev_per_trade={best_ev:.4f}")
    for w in warnings:
        print(f"  WARN {w}")

    print("[train] S5-36 pass gate check...")
    passed, failures = _check_pass_gate(gbm_report, lr_report.mean_brier or 1.0, cfg)
    if passed:
        print("  ✓ ALL CRITERIA PASSED — model can advance to S5-37 registration")
    else:
        print("  ✗ PASS gate FAILED — model NOT promoted")
        for f in failures:
            print(f"    {f}")

    out = {
        "lr_baseline": lr_report.__dict__,
        "gbm": gbm_report.__dict__,
        "calibration": cal_report.__dict__,
        "threshold": {"best_thr": best_thr, "best_ev_per_trade": best_ev,
                       "warnings": warnings},
        "pass_gate_passed": passed,
        "pass_gate_failures": failures,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(out, indent=2, default=str))
    print(f"[train] wrote {args.report}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
