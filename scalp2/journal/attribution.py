"""
journal/attribution.py — Attribution dashboard backend. Spec S5-82.

Per-strategy hit rate, R, Sharpe over rolling 30/90/365 day windows.
Built on decision_log + closed trades (Postgres §10.3).

Per-edge breakdown. Per-pass-reason histogram. Drift alerts (>10pp degradation)
surfaced visibly.

TODO Sprint 15.
"""
from __future__ import annotations


def rolling_metrics(strategy: str, window_days: int) -> dict: raise NotImplementedError("S5-82")
def per_pass_reason_histogram(strategy: str, window_days: int) -> dict: raise NotImplementedError("S5-82")
def drift_alerts(strategy: str) -> list[dict]: raise NotImplementedError("S5-82")
