"""Smoke tests for posterior_model.py (component 2 math primitives)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from posterior_model import _logit, _sigmoid, update_posterior


def test_logit_sigmoid_inverse():
    for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert abs(_sigmoid(_logit(p)) - p) < 1e-9, f"failed at p={p}"


def test_sigmoid_bounds():
    assert 0.0 < _sigmoid(-100) < 1e-30
    assert 1.0 - _sigmoid(100) < 1e-30


def test_logit_handles_extremes():
    # Should not raise / inf at p=0 or p=1
    _ = _logit(0.0)
    _ = _logit(1.0)


def test_positive_likelihood_with_positive_lambda_raises_posterior():
    p = update_posterior(prior=0.5, likelihoods={"uw": 0.5}, lambdas={"uw": 1.0})
    assert p > 0.5


def test_negative_likelihood_lowers_posterior():
    p = update_posterior(prior=0.5, likelihoods={"news": -0.5}, lambdas={"news": 1.0})
    assert p < 0.5


def test_zero_lambda_is_noop():
    p = update_posterior(prior=0.7, likelihoods={"uw": 0.5}, lambdas={"uw": 0.0})
    assert abs(p - 0.7) < 1e-9


def test_multiple_likelihoods_combine():
    p_one = update_posterior(prior=0.5, likelihoods={"uw": 0.3}, lambdas={"uw": 1.0})
    p_both = update_posterior(prior=0.5,
                              likelihoods={"uw": 0.3, "news": 0.3},
                              lambdas={"uw": 1.0, "news": 1.0})
    assert p_both > p_one, "two positive likelihoods should bump posterior more than one"


def test_unknown_likelihood_source_ignored():
    # A likelihood whose key isn't in lambdas should be skipped (no λ defined)
    p_with_extra = update_posterior(
        prior=0.5,
        likelihoods={"uw": 0.5, "unknown_source": 999.0},
        lambdas={"uw": 1.0},   # no λ for unknown_source
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
    print("[test_posterior_model] all tests passed")
