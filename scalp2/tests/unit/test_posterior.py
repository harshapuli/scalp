"""Smoke tests for strategies/s5_gamma_reversal/ml/posterior.py — S5-61."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from strategies.s5_gamma_reversal.ml.posterior import (
    DirectionalPosterior, make_priors, check_drift, check_divergence,
    _logit, _sigmoid, update_posterior,
)


# ──────────────────────────────────────────────────────────────────────────────
# Beta-Binomial DirectionalPosterior (S5-61)
# ──────────────────────────────────────────────────────────────────────────────


def test_priors_match_spec():
    """Spec §10.4 s5.posterior: long=10×0.55, short=10×0.50."""
    long_p, short_p = make_priors()
    assert long_p.alpha == 5.5 and long_p.beta == 4.5
    assert long_p.hit_rate == 5.5 / 10.0
    assert short_p.alpha == 5.0 and short_p.beta == 5.0
    assert short_p.hit_rate == 0.5


def test_update_increments_alpha_on_win():
    p = DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5)
    p.update(won=True)
    assert p.alpha == 6.5
    assert p.beta == 4.5


def test_update_increments_beta_on_loss():
    p = DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5)
    p.update(won=False)
    assert p.alpha == 5.5
    assert p.beta == 5.5


def test_posterior_converges_to_empirical():
    """S5-61.T1 — Inject 100 wins, 100 losses; converges to 0.50 within 1pp."""
    p = DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5)
    for _ in range(100):
        p.update(won=True)
    for _ in range(100):
        p.update(won=False)
    assert abs(p.hit_rate - 0.50) < 0.01


def test_directional_posteriors_independent():
    """S5-61.T2 — long-wins shouldn't move short posterior."""
    long_p, short_p = make_priors()
    for _ in range(100):
        long_p.update(won=True)
        short_p.update(won=False)
    assert long_p.hit_rate > 0.9, f"long should converge near 1.0, got {long_p.hit_rate}"
    assert short_p.hit_rate < 0.1, f"short should converge near 0.0, got {short_p.hit_rate}"


def test_check_drift_fires_on_drop():
    p = DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5)
    for _ in range(20):
        p.update(won=False)
    msg = check_drift(p, prior_hit_rate=0.55, drift_alert_pp=10.0)
    assert msg is not None
    assert "long_after_tank" in msg


def test_check_drift_silent_when_within_tolerance():
    p = DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5)
    msg = check_drift(p, prior_hit_rate=0.55, drift_alert_pp=10.0)
    assert msg is None


def test_check_divergence_fires_when_directions_diverge():
    long_p, short_p = make_priors()
    for _ in range(100):
        long_p.update(won=True)
        short_p.update(won=False)
    msg = check_divergence(long_p, short_p, direction_divergence_pp=15.0)
    assert msg is not None


def test_check_divergence_silent_when_aligned():
    long_p, short_p = make_priors()
    msg = check_divergence(long_p, short_p, direction_divergence_pp=15.0)
    assert msg is None


# ──────────────────────────────────────────────────────────────────────────────
# Logit-space update primitives (carry-forward, NOT S5-61)
# ──────────────────────────────────────────────────────────────────────────────


def test_logit_sigmoid_inverse():
    for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert abs(_sigmoid(_logit(p)) - p) < 1e-9


def test_update_posterior_positive_likelihood_raises():
    p = update_posterior(prior=0.5, likelihoods={"uw": 0.5}, lambdas={"uw": 1.0})
    assert p > 0.5


def test_update_posterior_unknown_source_ignored():
    p_with_extra = update_posterior(
        prior=0.5,
        likelihoods={"uw": 0.5, "unknown_source": 999.0},
        lambdas={"uw": 1.0},
    )
    p_without = update_posterior(
        prior=0.5, likelihoods={"uw": 0.5}, lambdas={"uw": 1.0},
    )
    assert abs(p_with_extra - p_without) < 1e-9


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("[test_posterior] all tests passed")
