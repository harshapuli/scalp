"""features/flow_features.py — SCALP-1.5.

net_signed_premium_5m / 30m, call_ask_pct, put_ask_pct, sweep_count, flow_flip,
signed_flow_score (z-score combo).

S4-1.T1 acceptance: pure call buy accumulation produces signed_flow_score >= 0.85.
S4-1.T2: balanced flow → near 0.

TODO Sprint 3.
"""
from __future__ import annotations

def net_signed_premium(records: list, window_minutes: int) -> float:
    raise NotImplementedError("SCALP-1.5")
def signed_flow_score(records: list, window_minutes: int = 30) -> float:
    raise NotImplementedError("SCALP-1.5")
