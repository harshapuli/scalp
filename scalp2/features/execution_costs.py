"""features/execution_costs.py — execution-aware features for ML.

bid_ask_spread_pct, option_volume_5d_avg, option_open_interest, option_spread_pct.

These feed S5-31 (training dataset with execution features) — the ML model
must reject signals killed by spread/liquidity/IV friction.

TODO Sprint 11.
"""
from __future__ import annotations

def bid_ask_spread_pct(bid: float, ask: float) -> float:
    if ask <= 0: return 0.0
    return (ask - bid) / ((ask + bid) / 2.0)
