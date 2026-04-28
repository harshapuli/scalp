"""
kelly_progression.py — component (4) of scalp 2.

Fractional Kelly with explicit ramp + 5% hard cap. Per DESIGN.md §3d.

  b = ev_expected_at_win / ev_expected_at_loss   # odds
  p = posterior
  q = 1 - p
  kelly_full = (b·p - q) / b

  scale_oos   = clip(realized_60d_wr / predicted_60d_wr, 0.0, 1.0)
  scale_phase = {OBSERVE: 0, PAPER: 0, LIVE_SMALL: 0.10, LIVE_FULL: 0.25}[state]
  kelly_used  = kelly_full · scale_oos · scale_phase

  HARD CAP: kelly_used ≤ 0.05  (5% per position, regardless of math)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from phase_gate import PhaseState

# Hard cap — never exceed regardless of math
KELLY_HARD_CAP = 0.05

# Per-state scale factor (DESIGN.md §3d)
PHASE_SCALE = {
    PhaseState.OBSERVE:    0.0,
    PhaseState.PAPER:      0.0,   # paper mode: log decisions but don't size
    PhaseState.LIVE_SMALL: 0.10,
    PhaseState.LIVE_FULL:  0.25,
}


@dataclass
class KellyDecision:
    kelly_full: float       # raw Kelly math result, can be negative
    scale_oos: float        # rolling out-of-sample win-rate alignment, [0, 1]
    scale_phase: float      # phase-state ramp, [0, 0.25]
    kelly_used: float       # final fraction (after caps), [0, KELLY_HARD_CAP]
    capped_by: Optional[str] = None    # "hard_cap" | "scale_oos" | "scale_phase" | None


def kelly_full_fraction(p: float, b: float) -> float:
    """Raw Kelly. p = win prob, b = odds (ev_win / |ev_loss|)."""
    if b <= 0:
        return 0.0
    return (b * p - (1 - p)) / b


def kelly_used(p: float, b: float, scale_oos: float, state: PhaseState) -> KellyDecision:
    full = kelly_full_fraction(p, b)
    sp = PHASE_SCALE.get(state, 0.0)

    # Negative Kelly => skip (don't trade against expected loss)
    if full <= 0:
        return KellyDecision(kelly_full=full, scale_oos=scale_oos, scale_phase=sp,
                             kelly_used=0.0, capped_by="negative_full")

    raw = full * max(0.0, min(1.0, scale_oos)) * sp
    capped_by = None
    if raw > KELLY_HARD_CAP:
        raw = KELLY_HARD_CAP
        capped_by = "hard_cap"
    return KellyDecision(kelly_full=full, scale_oos=scale_oos, scale_phase=sp,
                         kelly_used=raw, capped_by=capped_by)


# ──────────────────────────────────────────────────────────────────────────────
# Stubs — to implement in M4
# ──────────────────────────────────────────────────────────────────────────────


def compute_scale_oos(rolling_60d_realized_wr: float, rolling_60d_predicted_wr: float) -> float:
    """Out-of-sample alignment scale. Returns [0, 1]."""
    if rolling_60d_predicted_wr <= 0:
        return 0.0
    raw = rolling_60d_realized_wr / rolling_60d_predicted_wr
    return max(0.0, min(1.0, raw))


if __name__ == "__main__":
    # Sanity check
    d = kelly_used(p=0.65, b=2.0, scale_oos=0.9, state=PhaseState.LIVE_SMALL)
    print(f"[kelly] p=0.65 b=2.0 oos=0.9 LIVE_SMALL → kelly_used={d.kelly_used:.4f} (full={d.kelly_full:.4f})")

    d2 = kelly_used(p=0.95, b=5.0, scale_oos=1.0, state=PhaseState.LIVE_FULL)
    print(f"[kelly] p=0.95 b=5.0 oos=1.0 LIVE_FULL → kelly_used={d2.kelly_used:.4f} (capped_by={d2.capped_by})")
    assert d2.kelly_used <= KELLY_HARD_CAP, "hard cap violated"

    d3 = kelly_used(p=0.30, b=1.5, scale_oos=1.0, state=PhaseState.LIVE_SMALL)
    print(f"[kelly] p=0.30 b=1.5 (negative EV) → kelly_used={d3.kelly_used} (capped_by={d3.capped_by})")
    assert d3.kelly_used == 0.0, "negative-EV must result in zero size"

    print("[kelly] M1 scaffold OK — math primitives + hard cap enforced, M4 oos compute stubbed")
