"""
strategies/s5_gamma_reversal/execution/order_builder.py — Alpaca mleg constructor.

Per spec §10.5 build_s5_order pseudocode. Validates debit_to_width_max,
option_spread_pct_max gates BEFORE submission. Returns Decision (TRADE/PASS).

Tests required:
  FND-4.T1 — vertical leg construction (ratio_qty='1', position_intent set)

TODO Sprint 14.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from features.datatypes import Candidate, Decision, VerticalOrder


def build_s5_order(candidate: Candidate, chain, cfg: dict) -> Decision:
    """Spec §10.5. TODO Sprint 14."""
    raise NotImplementedError("Sprint 14")
