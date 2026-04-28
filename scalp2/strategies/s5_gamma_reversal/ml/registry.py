"""
strategies/s5_gamma_reversal/ml/registry.py — Model registry with version + hash + reproducibility.

Spec S5-37 + Postgres ml_models schema (§10.3):
  Stored: version, training_data_hash, feature_hash, code_git_sha, hparams,
          metrics (AUC, Brier, top_decile_p, calib_slope, feature_stab),
          threshold (EV-curve picked), calibration_curve, s3_path
  Registry persisted: Postgres + serialized model in S3
  Loading by version returns deterministic predictions

Tests required (S5-37.T1):
  - load(version) → byte-identical predictions on fixed sample (100 rows)

TODO Sprint 13.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ModelRecord:
    version: str                          # e.g. 's5_v1.0.3'
    strategy: str                         # 'S5'
    trained_at: datetime
    training_data_hash: str
    feature_hash: str
    code_git_sha: str
    hparams: dict
    metrics: dict                         # AUC, Brier, top_decile_p, calib_slope, feat_stab
    threshold: float                      # EV-curve picked
    calibration_curve: dict
    s3_path: str
    promoted_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None


class ModelRegistry:
    """S5-37. TODO Sprint 13."""

    def register(self, record: ModelRecord) -> None: raise NotImplementedError("S5-37")
    def load(self, version: str): raise NotImplementedError("S5-37")
    def list_active(self) -> list[ModelRecord]: raise NotImplementedError("S5-37")
    def promote(self, version: str) -> None: raise NotImplementedError("S5-37")
    def retire(self, version: str) -> None: raise NotImplementedError("S5-37")
