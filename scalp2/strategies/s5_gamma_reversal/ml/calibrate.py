"""strategies/s5_gamma_reversal/ml/calibrate.py — Probability calibration. S5-34.

Apply isotonic regression on validation fold. Verify:
  - Brier improves baseline by ≥ 10% (S5-34.T1)
  - Each probability decile contains ≥ 5 samples (S5-34.T2)
  - Calibration slope in [0.85, 1.15] (S5-34.T3)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss


@dataclass
class CalibrationReport:
    brier_uncalibrated: float
    brier_calibrated: float
    brier_improvement_pct: float          # positive → improvement
    reliability_bins: list[dict]           # per-decile: predicted, observed, n
    calibration_slope: float
    bins_with_min_samples: int
    n_bins: int = 10
    passed_brier_threshold: bool = False
    passed_bin_threshold: bool = False
    passed_slope_threshold: bool = False


class IsotonicCalibrator:
    """sklearn IsotonicRegression wrapper that exposes .predict_proba()
    so the calibrator slots into an ML pipeline cleanly.
    """

    def __init__(self):
        self._iso = IsotonicRegression(out_of_bounds="clip")

    def fit(self, raw_probas: np.ndarray, y_true: np.ndarray):
        self._iso.fit(raw_probas, y_true)
        return self

    def predict_proba(self, raw_probas: np.ndarray) -> np.ndarray:
        return np.asarray(self._iso.transform(raw_probas))


def reliability_bins(probas: np.ndarray, labels: np.ndarray, n_bins: int = 10
                      ) -> list[dict]:
    """S5-34. Bin predictions into deciles, count samples + observed rate per bin."""
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        mask = (probas >= lo) & (probas < hi)
        if i == n_bins - 1:
            mask = (probas >= lo) & (probas <= hi)
        n = int(mask.sum())
        if n == 0:
            out.append({"bin": i, "lo": float(lo), "hi": float(hi),
                        "n": 0, "predicted_mean": None, "observed_rate": None})
        else:
            out.append({
                "bin": i, "lo": float(lo), "hi": float(hi),
                "n": n,
                "predicted_mean": float(probas[mask].mean()),
                "observed_rate": float(labels[mask].mean()),
            })
    return out


def calibration_slope(probas: np.ndarray, labels: np.ndarray) -> float:
    """Linear regression of observed_p on predicted_p, slope only.
    Slope in [0.85, 1.15] is the spec acceptance band.
    """
    bins = reliability_bins(probas, labels)
    points = [(b["predicted_mean"], b["observed_rate"], b["n"])
              for b in bins if b["n"] > 0]
    if len(points) < 2:
        return 0.0
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    w = np.array([p[2] for p in points])
    # Weighted least squares: y = a + b*x
    n = w.sum()
    mx = (w * x).sum() / n
    my = (w * y).sum() / n
    cov = (w * (x - mx) * (y - my)).sum() / n
    var = (w * (x - mx) ** 2).sum() / n
    if var == 0:
        return 0.0
    return float(cov / var)


def calibrate_and_report(raw_probas: np.ndarray,
                          labels: np.ndarray,
                          n_bins: int = 10,
                          brier_improvement_required: float = 0.10,
                          min_samples_per_bin: int = 5,
                          slope_min: float = 0.85,
                          slope_max: float = 1.15) -> tuple[IsotonicCalibrator, CalibrationReport]:
    """S5-34. Fit isotonic calibrator + check all 3 acceptance bands."""
    calibrator = IsotonicCalibrator().fit(raw_probas, labels)
    cal_probas = calibrator.predict_proba(raw_probas)

    brier_un = float(brier_score_loss(labels, raw_probas))
    brier_cal = float(brier_score_loss(labels, cal_probas))
    improvement = (brier_un - brier_cal) / brier_un if brier_un > 0 else 0.0

    bins = reliability_bins(cal_probas, labels, n_bins=n_bins)
    bins_with_min = sum(1 for b in bins if b["n"] >= min_samples_per_bin)
    slope = calibration_slope(cal_probas, labels)

    report = CalibrationReport(
        brier_uncalibrated=brier_un,
        brier_calibrated=brier_cal,
        brier_improvement_pct=float(improvement),
        reliability_bins=bins,
        calibration_slope=slope,
        bins_with_min_samples=bins_with_min,
        n_bins=n_bins,
        passed_brier_threshold=improvement >= brier_improvement_required,
        passed_bin_threshold=bins_with_min >= n_bins,
        passed_slope_threshold=slope_min <= slope <= slope_max,
    )
    return calibrator, report
