"""Smoke tests for phase_gate.py (component 1 public surface)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phase_gate import (
    PhaseState, KindState, PhaseGateState, allow_s5_trade, PROMOTION
)


def test_unknown_kind_is_not_allowed():
    state = PhaseGateState()
    assert allow_s5_trade("UNKNOWN_KIND", state) is False


def test_observe_state_blocks_trade():
    state = PhaseGateState(states={
        "TEST_KIND": KindState(kind="TEST_KIND", state=PhaseState.OBSERVE),
    })
    assert allow_s5_trade("TEST_KIND", state) is False


def test_paper_state_blocks_trade():
    state = PhaseGateState(states={
        "TEST_KIND": KindState(kind="TEST_KIND", state=PhaseState.PAPER),
    })
    assert allow_s5_trade("TEST_KIND", state) is False


def test_live_small_allows_trade():
    state = PhaseGateState(states={
        "TEST_KIND": KindState(kind="TEST_KIND", state=PhaseState.LIVE_SMALL),
    })
    assert allow_s5_trade("TEST_KIND", state) is True


def test_live_full_allows_trade():
    state = PhaseGateState(states={
        "TEST_KIND": KindState(kind="TEST_KIND", state=PhaseState.LIVE_FULL),
    })
    assert allow_s5_trade("TEST_KIND", state) is True


def test_promotion_thresholds_increase():
    # Lift required to advance must increase as state advances
    o = PROMOTION[PhaseState.OBSERVE]["lift"]
    p = PROMOTION[PhaseState.PAPER]["lift"]
    s = PROMOTION[PhaseState.LIVE_SMALL]["lift"]
    assert o < p < s, f"promotion thresholds must be strictly increasing: {o},{p},{s}"


def test_promotion_n_min_increases():
    # n_min should not regress as state advances
    n_observe = PROMOTION[PhaseState.OBSERVE]["n_min"]
    n_paper = PROMOTION[PhaseState.PAPER]["n_min"]
    n_small = PROMOTION[PhaseState.LIVE_SMALL]["n_min"]
    assert n_paper >= n_observe
    assert n_small >= n_paper


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_phase_gate] all tests passed")
