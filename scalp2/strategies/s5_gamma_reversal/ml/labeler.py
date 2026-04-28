"""
strategies/s5_gamma_reversal/ml/labeler.py — Trade-outcome labeler. Spec S5-30.

Per ChatGPT review (incorporated): label_s5_trade_outcome (production label) +
label_reversal_next_N (sub-edge attribution, kept).

ACCEPTANCE CRITERIA (S5-30):
  - Renamed from label_s5_transition() — production label is TRADE OUTCOME
  - Label 1 ONLY if simulated production trade reaches target before stop
    AND survives modeled costs/slippage/IV-crush
  - Label 0 for: stop hit, timeout, IV crush failure, invalidation, OR unfilled/illiquid
  - Decision-time-only constraints: ATR, GEX, spread, IV all snapshotted at decision time
  - Future bars: bars[row.idx+1 : row.idx+1+N] strictly
  - Both labels stored: label_reversal_next_N (sub-edge) AND label_trade_success (prod)

Tests required (S5-30.T1, T2, T3):
  T1 — labeler rejects future-data ATR (raises LookAheadError)
  T2 — label_trade_success matches production execution (synthetic case)
  T3 — rejects winning reversal that loses money (IV crush case)

Canonical reference: spec §10.5 `label_s5_trade_outcome` pseudocode.

TODO Sprint 11.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from features.types import GEXSnapshot


class LookAheadError(Exception):
    """Raised when a feature/label uses data from after the decision time."""


def label_s5_trade_outcome(row, future_bars: list, direction: Literal["long_after_tank", "short_after_surge"],
                            decision_time_atr: float,
                            decision_time_gex: GEXSnapshot,
                            cost_model) -> tuple[int, int]:
    """S5-30. Returns (label_reversal_next_N, label_trade_success)."""
    raise NotImplementedError("S5-30 — TODO Sprint 11")


def decision_time_atr(bars: list, idx: int, period: int = 20) -> float:
    """S5-30.1. Asserts no future data accessed."""
    raise NotImplementedError("S5-30.1")


def decision_time_gex(gex_history: list[GEXSnapshot], timestamp) -> GEXSnapshot:
    """S5-30.2. Returns latest snapshot with ts ≤ timestamp."""
    raise NotImplementedError("S5-30.2")
