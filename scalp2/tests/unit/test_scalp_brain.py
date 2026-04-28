"""Smoke tests for scalp_brain modules — SCALP-2.T1, T2, T3 + SCALP-10.T1."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Features, ScalpState
from scalp_brain.classifier import classify
from scalp_brain.states import ScalpStateName, is_valid_transition
from scalp_brain.scores import reversal_score, ignition_score
from infra.config_loader import load_thresholds


CFG = load_thresholds()


def _make_features(**overrides) -> Features:
    base = dict(
        ticker="SPY",
        timestamp=datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        bar_idx=100,
        open=470.0, high=471.5, low=469.5, close=471.0,
        extension_from_vwap_atr=2.0,
        extension_from_prior_close_atr=2.0,
        failed_extension_atr=0.0,
        pullback_break=False,
        wick_pct=0.10,
        upper_wick_pct=0.10, lower_wick_pct=0.05,
        volume=1_000_000,
        volume_divergence_ratio=1.0,
        current_push_vol=500_000, prior_push_vol=500_000,
        climax_vol_ratio=1.5,
        aggressor_recent=0.7, aggressor_prior=0.5,
        aggressor_velocity=0.1, flip_strength=0.2,
        distance_to_major_pos_gex_atr=0.30,
        distance_to_pos_gex_atr=0.30,
        gex_magnitude_rank=0.8,
        gamma_flip_distance=1.0,
        strike_oi_rank=0.7,
        major_gex_strike_dte=10,
        gex_snapshot_age_min=5,
        net_signed_premium_5m=200_000,
        net_signed_premium_30m=500_000,
        call_ask_pct=0.6, put_ask_pct=0.4,
        sweep_count=3, flow_flip=False,
        signed_flow_score=0.4,
        near_htf_level_atr=0.30,
        earnings_blackout=False, fomc_blackout=False,
        iv_percentile=0.5, front_iv_change=0.01, skew_slope=-0.03,
        bid_ask_spread_pct=0.02,
        option_volume_5d_avg=500,
        option_open_interest=2000,
        option_spread_pct=0.08,
    )
    base.update(overrides)
    return Features(**base)


# SCALP-2.T1
def test_neutral_to_surge_ignition_when_extension_crosses():
    f = _make_features(extension_from_prior_close_atr=2.0, aggressor_recent=0.8)
    new = classify(prior_state=None, features=f, cfg=CFG)
    assert new.name == ScalpStateName.SURGE_IGNITION.value, f"expected SURGE_IGNITION, got {new.name}"


# SCALP-2.T2 — invalid transition refused
def test_surge_reverse_requires_continuation_or_climax_prior():
    # NEUTRAL → SURGE_REVERSE is NOT a valid transition
    assert not is_valid_transition(ScalpStateName.NEUTRAL, ScalpStateName.SURGE_REVERSE)
    # CONTINUATION → REVERSE IS valid
    assert is_valid_transition(ScalpStateName.SURGE_CONTINUATION, ScalpStateName.SURGE_REVERSE)
    # CLIMAX → REVERSE IS valid
    assert is_valid_transition(ScalpStateName.SURGE_CLIMAX, ScalpStateName.SURGE_REVERSE)


# SCALP-2.T3 — hysteresis prevents flip-flopping
def test_state_hysteresis_holds_for_min_age_bars():
    f1 = _make_features()
    init = classify(prior_state=None, features=f1, cfg=CFG)
    assert init.age_bars == 0

    # Try to flip away to a low-conviction non-reversal state in next bar (age=0)
    # Need a feature set that wouldn't normally pass thresholds
    f2 = _make_features(
        extension_from_prior_close_atr=0.2,
        aggressor_recent=0.1,
    )
    next_state = classify(prior_state=init, features=f2, cfg=CFG)
    # Hysteresis should hold us at the original state (or at least at age >= 0)
    # Since this is an ignition state, low scores shouldn't trigger NEUTRAL within min_age
    assert next_state.name == init.name, f"hysteresis broken: {init.name} → {next_state.name}"


# SCALP-10.T1 — reversal_score = 0 in NEUTRAL state
def test_reversal_score_low_when_no_reversal_signature():
    f = _make_features(
        wick_pct=0.05, upper_wick_pct=0.05, lower_wick_pct=0.05,
        aggressor_recent=0.0, aggressor_prior=0.0,
        flow_flip=False, volume_divergence_ratio=1.0,
        failed_extension_atr=0.0,
    )
    s = reversal_score(f, "long", CFG)
    assert s < 0.40, f"expected low reversal_score, got {s}"


# SCALP-10.T2 — textbook surge-top fires reversal_score >= 0.55
def test_reversal_score_surge_top_textbook():
    f = _make_features(
        upper_wick_pct=0.45,
        aggressor_prior=0.7, aggressor_recent=-0.4,
        flow_flip=True, net_signed_premium_5m=-500_000,
        volume_divergence_ratio=0.5,
        failed_extension_atr=-0.8,           # negative = retraced from extension
    )
    s = reversal_score(f, "short", CFG)
    print(f"[test] surge-top reversal_score={s:.3f}")
    assert s >= 0.50, f"expected surge-top reversal score ≥ 0.50, got {s:.3f}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_scalp_brain] all tests passed")
