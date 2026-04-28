"""
phase_gate.py — component (1) of scalp 2.

State machine per signal kind: OBSERVE → PAPER → LIVE_SMALL → LIVE_FULL.
Public surface: allow_s5_trade(kind, ts) → bool.

Per DESIGN.md §3a + §4. Inferred from scalp 1's references; thresholds may need
reconciliation if trading_system_lifecycle.docx surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PHASE_STATE_DIR = PROJECT_ROOT / "data" / "phase_state"


class PhaseState(str, Enum):
    OBSERVE = "OBSERVE"
    PAPER = "PAPER"
    LIVE_SMALL = "LIVE_SMALL"
    LIVE_FULL = "LIVE_FULL"


# Promotion thresholds — DESIGN.md §4. Tuneable.
PROMOTION = {
    PhaseState.OBSERVE:    {"to": PhaseState.PAPER,      "lift": 1.10, "n_min": 30},
    PhaseState.PAPER:      {"to": PhaseState.LIVE_SMALL, "lift": 1.15, "n_min": 30},
    PhaseState.LIVE_SMALL: {"to": PhaseState.LIVE_FULL,  "lift": 1.20, "n_min": 60},
}

# Demotion: any state regresses one step on rolling 7d drawdown > 2× expected EV
DEMOTION_DRAWDOWN_MULTIPLIER = 2.0
DEMOTION_LOOKBACK_DAYS = 7

# Window for promotion lift calculation
PROMOTION_LOOKBACK_DAYS = 30


@dataclass
class KindState:
    """Per-signal-kind phase state."""
    kind: str
    state: PhaseState = PhaseState.OBSERVE
    since_utc: str = ""
    rolling_30d_lift: Optional[float] = None
    rolling_30d_n: int = 0
    rolling_7d_drawdown_pct: Optional[float] = None
    expected_ev_pct: Optional[float] = None
    next_review_utc: Optional[str] = None


@dataclass
class PhaseGateState:
    generated_utc: str = ""
    states: dict[str, KindState] = field(default_factory=dict)


def allow_s5_trade(kind: str, state: PhaseGateState, ts: Optional[datetime] = None) -> bool:
    """Public gating function. Returns True iff the kind is in LIVE_SMALL or LIVE_FULL."""
    ks = state.states.get(kind)
    if ks is None:
        return False
    return ks.state in (PhaseState.LIVE_SMALL, PhaseState.LIVE_FULL)


# ──────────────────────────────────────────────────────────────────────────────
# Stubs — to implement in M4
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_promotion(ks: KindState) -> Optional[PhaseState]:
    """Returns target state if promotion criteria met, else None.

    TODO M4: implement per DESIGN.md §4.
    """
    raise NotImplementedError("M4 deliverable")


def evaluate_demotion(ks: KindState) -> Optional[PhaseState]:
    """Returns target state if demotion criteria met, else None.

    TODO M4: implement per DESIGN.md §4 (rolling 7d drawdown > 2× expected EV).
    """
    raise NotImplementedError("M4 deliverable")


def update_phase(ks: KindState) -> KindState:
    """Apply promotion/demotion logic, return updated KindState."""
    raise NotImplementedError("M4 deliverable")


if __name__ == "__main__":
    print("[phase_gate] M1 scaffold — public surface declared, M4 logic stubbed")
    print(f"[phase_gate] PROMOTION thresholds: {PROMOTION}")
