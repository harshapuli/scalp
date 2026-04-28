"""Smoke tests for swing_brain.lifecycle — SWING-1.T1, T2."""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Candidate, Features
from swing_brain.lifecycle import transition, can_transition, InvalidTransitionError


def _stub_candidate(state="SETUP") -> Candidate:
    f = Features(
        ticker="SPY",
        timestamp=datetime(2026, 4, 28, tzinfo=timezone.utc), bar_idx=0,
        open=470, high=471, low=469, close=470,
        extension_from_vwap_atr=2, extension_from_prior_close_atr=2,
        failed_extension_atr=0, pullback_break=False,
        wick_pct=0.1, upper_wick_pct=0.1, lower_wick_pct=0.05,
        volume=1_000_000, volume_divergence_ratio=1.0,
        current_push_vol=500_000, prior_push_vol=500_000, climax_vol_ratio=1.0,
        aggressor_recent=0.5, aggressor_prior=0.4,
        aggressor_velocity=0.1, flip_strength=0.1,
        distance_to_major_pos_gex_atr=0.3, distance_to_pos_gex_atr=0.3,
        gex_magnitude_rank=0.7, gamma_flip_distance=1.0, strike_oi_rank=0.7,
        major_gex_strike_dte=10, gex_snapshot_age_min=5,
        net_signed_premium_5m=200_000, net_signed_premium_30m=500_000,
        call_ask_pct=0.6, put_ask_pct=0.4, sweep_count=3, flow_flip=False,
        signed_flow_score=0.4, near_htf_level_atr=0.3,
        earnings_blackout=False, fomc_blackout=False,
        iv_percentile=0.5, front_iv_change=0.01, skew_slope=-0.03,
        bid_ask_spread_pct=0.02, option_volume_5d_avg=500,
        option_open_interest=2000, option_spread_pct=0.08,
    )
    return Candidate(
        id=str(uuid.uuid4()), strategy="S5", ticker="SPY",
        direction="long", state=state,
        created_at=datetime.now(tz=timezone.utc),
        expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=30),
        setup_features=f, setup_features_hash=f.hash(),
        atr=2.0, gamma_strike=475.0, spread_width=5.0,
    )


# SWING-1.T1
def test_invalid_transition_raises():
    c = _stub_candidate(state="SETUP")
    try:
        transition(c, "MANAGE", triggered_by="manual")
        raise AssertionError("expected InvalidTransitionError")
    except InvalidTransitionError as e:
        assert "SETUP" in str(e) and "MANAGE" in str(e)


# SWING-1.T2
def test_full_lifecycle_logs_six_transitions():
    c = _stub_candidate(state="INDUCEMENT")
    sequence = [
        ("SETUP", "scanner"),
        ("TRIGGER", "scalp.price_trigger"),
        ("ENTRY", "allow_s5_trade"),
        ("MANAGE", "order_filled"),
        ("EXIT", "target_hit"),
        ("JOURNAL", "exit_finalized"),
    ]
    for to_state, trig in sequence:
        c = transition(c, to_state, triggered_by=trig)
    assert len(c.transition_log) == 6, f"expected 6 transitions, got {len(c.transition_log)}"
    assert c.state == "JOURNAL"
    assert c.predecessor_states == [
        "INDUCEMENT", "SETUP", "TRIGGER", "ENTRY", "MANAGE", "EXIT",
    ]


def test_can_transition_predicate_matches_table():
    assert can_transition("INDUCEMENT", "SETUP")
    assert not can_transition("INDUCEMENT", "TRIGGER")
    assert can_transition("MANAGE", "EXIT")
    assert not can_transition("MANAGE", "JOURNAL")    # must go EXIT first
    assert not can_transition("JOURNAL", "INDUCEMENT")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_swing_brain] all tests passed")
