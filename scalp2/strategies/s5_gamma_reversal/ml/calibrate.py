"""
strategies/s5_gamma_reversal/ml/calibrate.py — Probability calibration. Spec S5-34.

Apply isotonic regression on validation fold. Verify:
  - Brier improves baseline by ≥ 10% (S5-34.T1)
  - Each probability decile contains ≥ 5 samples (S5-34.T2)
  - Calibration slope in [0.85, 1.15] (S5-34.T3)

The slope check matters because calibration could move predictions to the safe
middle (compressing variance) — that improves Brier but kills decision-making.

TODO Sprint 12.
"""
from __future__ import annotations


def isotonic_calibrate(probas, labels):
    """S5-34. TODO Sprint 12. Returns CalibratedClassifierCV-style object."""
    raise NotImplementedError("S5-34")


def reliability_bins(probas, labels, n_bins: int = 10):
    """Bin predictions into deciles, count samples + observed rate per bin."""
    raise NotImplementedError("S5-34")


def calibration_slope(probas, labels) -> float:
    """Linear regression of observed_p on predicted_p; slope in [0.85, 1.15] required."""
    raise NotImplementedError("S5-34")
