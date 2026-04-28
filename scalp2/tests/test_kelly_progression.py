"""Smoke tests for kelly_progression.py (component 4 math primitives)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kelly_progression import (
    kelly_full_fraction, kelly_used, KELLY_HARD_CAP, PHASE_SCALE
)
from phase_gate import PhaseState


def test_kelly_full_positive_when_edge_exists():
    # 65% win, 2:1 odds → strong positive Kelly
    k = kelly_full_fraction(p=0.65, b=2.0)
    assert k > 0, f"expected positive Kelly, got {k}"
    # Closed form: (2*0.65 - 0.35)/2 = (1.3-0.35)/2 = 0.475
    assert abs(k - 0.475) < 1e-9


def test_kelly_full_negative_when_no_edge():
    # 30% win, 1.5:1 odds → negative Kelly
    k = kelly_full_fraction(p=0.30, b=1.5)
    assert k < 0


def test_negative_full_means_zero_used():
    d = kelly_used(p=0.30, b=1.5, scale_oos=1.0, state=PhaseState.LIVE_FULL)
    assert d.kelly_used == 0.0
    assert d.capped_by == "negative_full"


def test_observe_state_yields_zero():
    # Even with strong edge, OBSERVE state means no live size
    d = kelly_used(p=0.80, b=3.0, scale_oos=1.0, state=PhaseState.OBSERVE)
    assert d.kelly_used == 0.0


def test_paper_state_yields_zero():
    d = kelly_used(p=0.80, b=3.0, scale_oos=1.0, state=PhaseState.PAPER)
    assert d.kelly_used == 0.0


def test_live_small_uses_010_scale():
    # full=0.5, oos=1.0, scale=0.10 → kelly_used=0.05 — exactly at cap
    d = kelly_used(p=0.75, b=3.0, scale_oos=1.0, state=PhaseState.LIVE_SMALL)
    # full = (3*0.75 - 0.25)/3 = (2.25-0.25)/3 = 0.6667
    # raw = 0.6667 * 1.0 * 0.10 = 0.06667 → capped to 0.05
    assert d.kelly_used == KELLY_HARD_CAP
    assert d.capped_by == "hard_cap"


def test_hard_cap_enforced():
    # Pathological case: very high prob + very high odds + LIVE_FULL
    d = kelly_used(p=0.95, b=10.0, scale_oos=1.0, state=PhaseState.LIVE_FULL)
    assert d.kelly_used <= KELLY_HARD_CAP, f"hard cap violated: {d.kelly_used}"


def test_oos_scale_clipped_to_unit_interval():
    d_low = kelly_used(p=0.65, b=2.0, scale_oos=-0.5, state=PhaseState.LIVE_SMALL)
    assert d_low.kelly_used == 0.0
    d_high = kelly_used(p=0.65, b=2.0, scale_oos=2.5, state=PhaseState.LIVE_SMALL)
    # Should clip to 1.0, not multiply by 2.5
    d_unit = kelly_used(p=0.65, b=2.0, scale_oos=1.0, state=PhaseState.LIVE_SMALL)
    assert d_high.kelly_used == d_unit.kelly_used


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_kelly_progression] all tests passed")
