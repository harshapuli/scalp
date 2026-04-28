"""Smoke tests for risk/sizing.py — fractional Kelly + ramp + per-trade cap."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from risk.sizing import (
    kelly_full_fraction, compute_size, compute_scale_oos,
    PER_TRADE_PCT_PAPER, PER_TRADE_PCT_FULL,
    RAMP_P1, RAMP_P2, RAMP_FULL,
    TradingMode,
)


def test_kelly_full_positive_when_edge_exists():
    # 65% win, 2:1 odds → strong positive Kelly
    k = kelly_full_fraction(p=0.65, b=2.0)
    assert k > 0
    # Closed form: (2*0.65 - 0.35)/2 = 0.475
    assert abs(k - 0.475) < 1e-9


def test_kelly_full_negative_when_no_edge():
    k = kelly_full_fraction(p=0.30, b=1.5)
    assert k < 0


def test_kelly_full_zero_when_b_invalid():
    assert kelly_full_fraction(p=0.5, b=0) == 0.0
    assert kelly_full_fraction(p=0.5, b=-1.0) == 0.0


def test_negative_kelly_means_zero_size():
    d = compute_size(p=0.30, b=1.5, scale_oos=1.0, mode=TradingMode.FULL)
    assert d.pos_size_pct == 0.0
    assert d.capped_by == "negative_full"


def test_paper_mode_yields_zero_size():
    # Even with strong edge, PAPER mode logs only — never sizes live
    d = compute_size(p=0.80, b=3.0, scale_oos=1.0, mode=TradingMode.PAPER)
    assert d.pos_size_pct == 0.0
    assert d.capped_by == "paper_mode"
    # But the math is computed for visibility
    assert d.kelly_full > 0


def test_ramp_25_uses_paper_cap():
    # In RAMP_25, cap is still per_trade_pct_paper (0.5%)
    d = compute_size(p=0.75, b=3.0, scale_oos=1.0, mode=TradingMode.RAMP_25)
    # full = (3*0.75 - 0.25)/3 ≈ 0.6667
    # raw = 0.6667 * 1.0 * 0.25 = 0.1667 → capped at 0.005
    assert d.pos_size_pct == PER_TRADE_PCT_PAPER
    assert d.capped_by == "hard_cap"
    assert d.scale_phase == RAMP_P1


def test_ramp_50_uses_paper_cap():
    d = compute_size(p=0.75, b=3.0, scale_oos=1.0, mode=TradingMode.RAMP_50)
    assert d.pos_size_pct == PER_TRADE_PCT_PAPER
    assert d.scale_phase == RAMP_P2


def test_full_mode_uses_full_cap():
    # FULL mode allows up to 1.0% per trade
    d = compute_size(p=0.95, b=10.0, scale_oos=1.0, mode=TradingMode.FULL)
    assert d.pos_size_pct == PER_TRADE_PCT_FULL
    assert d.capped_by == "hard_cap"
    assert d.scale_phase == RAMP_FULL
    assert d.hard_cap == PER_TRADE_PCT_FULL


def test_oos_scale_clipped_to_unit_interval():
    # Negative oos → 0 size
    d_low = compute_size(p=0.65, b=2.0, scale_oos=-0.5, mode=TradingMode.RAMP_25)
    assert d_low.pos_size_pct == 0.0
    # >1 oos → clipped to 1, not amplified
    d_high = compute_size(p=0.65, b=2.0, scale_oos=2.5, mode=TradingMode.RAMP_25)
    d_unit = compute_size(p=0.65, b=2.0, scale_oos=1.0, mode=TradingMode.RAMP_25)
    assert d_high.pos_size_pct == d_unit.pos_size_pct


def test_caps_match_spec():
    # Spec §10.4 s5.risk: per_trade_pct_paper=0.005, per_trade_pct_full=0.010
    assert PER_TRADE_PCT_PAPER == 0.005
    assert PER_TRADE_PCT_FULL == 0.010


def test_ramp_multipliers_match_spec():
    # Spec §10.4 s5.ramp: sizing_p1=0.25, sizing_p2=0.50, sizing_full=1.00
    assert RAMP_P1 == 0.25
    assert RAMP_P2 == 0.50
    assert RAMP_FULL == 1.00


def test_compute_scale_oos_basic():
    assert compute_scale_oos(0.55, 0.55) == 1.0     # exact match → full size
    assert compute_scale_oos(0.275, 0.55) == 0.5    # half realized → half size
    assert compute_scale_oos(0.825, 0.55) == 1.0    # exceeds → clip at 1
    assert compute_scale_oos(0.0, 0.55) == 0.0      # zero realized → zero
    assert compute_scale_oos(0.55, 0.0) == 0.0      # zero predicted → zero


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_sizing] all tests passed")
