"""
scalp_brain/replay.py — Historical replay harness. Spec SCALP-20 + SCALP-21.

Acceptance criteria:
  - Given a date range, replay produces identical state sequence each run (SCALP-20.T1)
  - All inputs from S3-cached fixtures, not live API
  - Output: per-bar (state, score, features_hash) consumable by downstream
  - Replay 12mo top-100 names completes in < 1 hour
  - Live ↔ replay parity: 0 bars different per trading day (SCALP-21.T1)

TODO Sprint 5.
"""
from __future__ import annotations


def replay_date_range(start: str, end: str, universe: list[str], cfg: dict):
    """SCALP-20. TODO Sprint 5."""
    raise NotImplementedError("SCALP-20")


def parity_check(date: str, live_states_path: str, fixture_path: str) -> int:
    """SCALP-21. Returns count of bars where live ≠ replay. Must be 0 to advance."""
    raise NotImplementedError("SCALP-21")
