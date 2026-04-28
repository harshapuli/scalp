"""Smoke tests for strategies/s5_gamma_reversal/ml/threshold.py (S5-35)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from strategies.s5_gamma_reversal.ml.threshold import (
    EVCurve, EVCurvePoint, ev_lookup, DEFAULT_PERCENTILES,
)


def _synth_curve(slope=0.01, intercept=-0.5):
    """Linear EV curve: ev_mean = slope * percentile + intercept."""
    pts = [EVCurvePoint(percentile=p,
                         ev_mean=slope * p + intercept,
                         ev_p25=slope * p + intercept - 0.2,
                         ev_p75=slope * p + intercept + 0.2,
                         n=10)
           for p in DEFAULT_PERCENTILES]
    return EVCurve(kind="SYNTH", fitted_at_utc="now", n_archive_fires=100, points=pts)


def test_lookup_at_anchor_points():
    c = _synth_curve()
    # At p=5 (first anchor), ev_mean should be 0.01*5 - 0.5 = -0.45
    assert abs(ev_lookup(c, 5) - (-0.45)) < 1e-9


def test_lookup_below_min_clamps():
    c = _synth_curve()
    # Below p=5 (min anchor), should return p=5 value
    assert ev_lookup(c, 0) == ev_lookup(c, 5)


def test_lookup_above_max_clamps():
    c = _synth_curve()
    # Above p=95 (max anchor), should return p=95 value
    assert ev_lookup(c, 100) == ev_lookup(c, 95)


def test_lookup_interpolates_between_anchors():
    c = _synth_curve()
    # p=10 is exactly between anchors p=5 and p=15
    # synth values: p=5 → -0.45, p=15 → -0.35; midpoint should be -0.40
    val = ev_lookup(c, 10)
    assert abs(val - (-0.40)) < 1e-6, f"got {val}"


def test_p25_and_p75_keys():
    c = _synth_curve()
    p25 = ev_lookup(c, 50, "ev_p25")
    p75 = ev_lookup(c, 50, "ev_p75")
    mean = ev_lookup(c, 50, "ev_mean")
    assert p25 < mean < p75, f"p25={p25} mean={mean} p75={p75} not ordered"


def test_empty_curve_returns_none():
    c = EVCurve(kind="EMPTY", fitted_at_utc="now", n_archive_fires=0, points=[])
    assert ev_lookup(c, 50) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_ev_curve] all tests passed")
