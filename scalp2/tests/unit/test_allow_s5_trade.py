"""Smoke tests for strategies/s5_gamma_reversal/allow_s5_trade.py — S5-104.

Per spec §10.5 + §10.4. Each gate has a unique pass_reason; first failure
short-circuits. Validates gate ordering against the locked sequence.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from strategies.s5_gamma_reversal.allow_s5_trade import (
    allow_s5_trade, PASS_REASONS,
)
from features.datatypes import Features


# Minimal config matching spec §10.4 s5.* tree
TEST_CFG = {
    "s5": {
        "setup": {
            "near_gex_atr": 0.50,
            "extended_atr_min": 1.50,
            "at_liquidity_atr": 0.50,
            "gex_strike_dte_min": 6,
            "gex_snapshot_max_age_min": 30,
        },
        "risk": {
            "bid_ask_spread_pct_max": 0.05,
            "option_volume_5d_min": 200,
        },
    },
}


def _make_passing_features(**overrides) -> Features:
    """Builds Features that pass every gate. Override one to test that gate."""
    base = dict(
        ticker="SPY",
        timestamp=datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        bar_idx=100,
        open=470.0, high=471.0, low=469.5, close=470.5,
        extension_from_vwap_atr=2.0,                 # extended ✓
        extension_from_prior_close_atr=2.0,
        failed_extension_atr=0.0,
        pullback_break=False,
        wick_pct=0.20,
        upper_wick_pct=0.20, lower_wick_pct=0.05,
        volume=1_000_000,
        volume_divergence_ratio=1.0,
        current_push_vol=500_000, prior_push_vol=500_000,
        climax_vol_ratio=1.0,
        aggressor_recent=0.6, aggressor_prior=0.5,
        aggressor_velocity=0.1, flip_strength=0.2,
        distance_to_major_pos_gex_atr=0.30,          # near gex ✓
        distance_to_pos_gex_atr=0.30,
        gex_magnitude_rank=0.8,
        gamma_flip_distance=1.0,
        strike_oi_rank=0.7,
        major_gex_strike_dte=10,                     # > 6, OK
        gex_snapshot_age_min=15,                     # < 30, OK
        net_signed_premium_5m=200_000,
        net_signed_premium_30m=500_000,
        call_ask_pct=0.6, put_ask_pct=0.4,
        sweep_count=3, flow_flip=False,
        signed_flow_score=0.4,
        near_htf_level_atr=0.30,                     # < 0.50, at liquidity ✓
        earnings_blackout=False,
        fomc_blackout=False,
        iv_percentile=0.5,
        front_iv_change=0.01,
        skew_slope=-0.03,
        bid_ask_spread_pct=0.02,                     # < 0.05, OK
        option_volume_5d_avg=500,                    # > 200, OK
        option_open_interest=2000,
        option_spread_pct=0.08,
    )
    base.update(overrides)
    return Features(**base)


def test_passing_features_yield_trade():
    f = _make_passing_features()
    d = allow_s5_trade(
        features=f, model=None, threshold=0.50, cfg=TEST_CFG,
        risk_manager_allows=True,
        expected_value_net=0.05,
        reversal_score=0.60, required_reversal_score=0.50,
    )
    assert d.decision == "TRADE", f"expected TRADE, got {d.decision} ({d.pass_reason})"
    assert d.strategy == "S5"


def test_gate1_not_near_gex():
    f = _make_passing_features(distance_to_major_pos_gex_atr=0.80,
                               distance_to_pos_gex_atr=0.80)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.decision == "PASS"
    assert d.pass_reason == "not_near_gex"


def test_gate1_not_extended():
    f = _make_passing_features(extension_from_vwap_atr=0.5,
                               extension_from_prior_close_atr=0.5)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "not_extended"


def test_gate1_not_at_liquidity():
    f = _make_passing_features(near_htf_level_atr=0.80)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "not_at_liquidity"


def test_gate1_event_blackout():
    f = _make_passing_features(earnings_blackout=True)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "event_blackout"


def test_gate1_gex_strike_too_close_to_expiry():
    """v2.2 §22b OPEX guard: major_gex_strike_dte < 6 → reject."""
    f = _make_passing_features(major_gex_strike_dte=4)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "gex_strike_too_close_to_expiry"


def test_gate1_gex_stale():
    """v2.2 §22a freshness: gex_snapshot_age_min > 30 → reject."""
    f = _make_passing_features(gex_snapshot_age_min=45)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "gex_stale"


def test_gate2_reversal_score_too_low():
    f = _make_passing_features()
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05,
                       reversal_score=0.30, required_reversal_score=0.50)
    assert d.pass_reason == "reversal_score_too_low"


def test_gate6_option_spread_too_wide():
    f = _make_passing_features(bid_ask_spread_pct=0.08)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "option_spread_too_wide"


def test_gate7_option_liquidity_too_low():
    f = _make_passing_features(option_volume_5d_avg=100)
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "option_liquidity_too_low"


def test_gate9_negative_ev():
    f = _make_passing_features()
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, True,
                       expected_value_net=-0.02, reversal_score=0.60)
    assert d.pass_reason == "negative_ev"


def test_gate10_risk_denied():
    f = _make_passing_features()
    d = allow_s5_trade(f, None, 0.50, TEST_CFG, risk_manager_allows=False,
                       expected_value_net=0.05, reversal_score=0.60)
    assert d.pass_reason == "risk_denied"


def test_pass_reasons_unique():
    """S5-104.T1 — every gate has a UNIQUE pass_reason."""
    assert len(PASS_REASONS) == len(set(PASS_REASONS))


def test_pass_reasons_non_empty_strings():
    for r in PASS_REASONS:
        assert isinstance(r, str) and len(r) > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_allow_s5_trade] all tests passed")
