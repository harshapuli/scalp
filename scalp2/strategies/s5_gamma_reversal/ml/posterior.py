"""
strategies/s5_gamma_reversal/ml/posterior.py — Per-direction posterior tracking (S5-61).

Per spec §3.6 (S5-61) and §10.5 (DirectionalPosterior pseudocode):

> TWO Beta-Binomial trackers (NOT single):
>   long_after_tank:    alpha=10×0.55, beta=10×0.45
>   short_after_surge:  alpha=10×0.50, beta=10×0.50
> Each direction's posterior used in EV calc for THAT direction's trades only.
> Alert if either drops > 10pp below prior.
> Alert if posteriors diverge > 15pp from each other — indicates asymmetric
> edge or bug.

Two callable surfaces:
  - DirectionalPosterior dataclass with .update(won) + .hit_rate
  - check_drift(post, prior_hit_rate, cfg) — fires alert if > drift_alert_pp

Logit-space Bayesian update (from inferred scaffold) is kept as a free
function for completeness, but is NOT what S5-61 specifies. Use it only
if a non-conjugate likelihood needs to be combined with a Bernoulli prior
(e.g., scalp 1's GBM score combined with live UW/news/GEX likelihoods —
that's a different layer, not S5-61's per-direction tracker).

NOTE: replaces inferred scaffold formerly at scalp2/src/posterior_model.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Literal, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Beta-Binomial per-direction tracker (S5-61)
# ──────────────────────────────────────────────────────────────────────────────


Direction = Literal["long_after_tank", "short_after_surge"]


@dataclass
class DirectionalPosterior:
    """Beta-Binomial tracker for one trade direction.

    Per spec §10.5 + S5-61 acceptance criteria. Initial priors:
      long_after_tank:    alpha=10×0.55=5.5, beta=10×0.45=4.5
      short_after_surge:  alpha=10×0.50=5.0, beta=10×0.50=5.0
    """
    direction: Direction
    alpha: float
    beta: float

    @property
    def hit_rate(self) -> float:
        """Posterior mean of Beta(alpha, beta)."""
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    def update(self, won: bool) -> None:
        """Bayesian update with one Bernoulli observation."""
        if won:
            self.alpha += 1
        else:
            self.beta += 1

    def divergence_from(self, other: "DirectionalPosterior") -> float:
        """Absolute difference in hit rates. Used for direction_divergence_pp alert."""
        return abs(self.hit_rate - other.hit_rate)

    def n_observations(self) -> float:
        """Total observations including prior pseudo-counts."""
        return self.alpha + self.beta


def make_priors() -> tuple[DirectionalPosterior, DirectionalPosterior]:
    """Construct the two priors per spec §10.4 s5.posterior."""
    return (
        DirectionalPosterior(direction="long_after_tank", alpha=5.5, beta=4.5),
        DirectionalPosterior(direction="short_after_surge", alpha=5.0, beta=5.0),
    )


def check_drift(post: DirectionalPosterior,
                prior_hit_rate: float,
                drift_alert_pp: float = 10.0,
                alert_fn: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Returns alert message if drift exceeds threshold, else None.

    Per spec §10.5: 'alert(f"{post.direction} posterior dropped {drop_pp:.1f}pp")'
    """
    drop_pp = (prior_hit_rate - post.hit_rate) * 100.0
    if drop_pp > drift_alert_pp:
        msg = f"{post.direction} posterior dropped {drop_pp:.1f}pp from prior {prior_hit_rate:.2f} → {post.hit_rate:.2f}"
        if alert_fn is not None:
            alert_fn(msg)
        return msg
    return None


def check_divergence(long_post: DirectionalPosterior,
                     short_post: DirectionalPosterior,
                     direction_divergence_pp: float = 15.0,
                     alert_fn: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Returns alert if directional posteriors diverge > threshold (S5-61)."""
    div = long_post.divergence_from(short_post) * 100.0
    if div > direction_divergence_pp:
        msg = (f"directional posteriors diverge {div:.1f}pp "
               f"(long={long_post.hit_rate:.2f}, short={short_post.hit_rate:.2f}) "
               f"— check for asymmetric edge or bug")
        if alert_fn is not None:
            alert_fn(msg)
        return msg
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Logit-space Bayesian update (free function — not S5-61)
# ──────────────────────────────────────────────────────────────────────────────


def _logit(p: float, eps: float = 1e-6) -> float:
    p = max(eps, min(1 - eps, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def update_posterior(prior: float,
                     likelihoods: dict[str, float],
                     lambdas: dict[str, float]) -> float:
    """Bayesian update in logit space.

    NOT S5-61. Used when combining scalp 1 GBM score with live UW/news/GEX
    likelihoods (per inferred scaffold §3b) — a candidate for a future
    `live_score_model.py` if/when that fusion layer is built.

    likelihoods: {source_name: signed_score}
    lambdas:     {source_name: weight} — fit by MLE, currently unused
    """
    z = _logit(prior)
    for src, like in likelihoods.items():
        lam = lambdas.get(src, 0.0)
        z += lam * like
    return _sigmoid(z)


if __name__ == "__main__":
    # Sanity per S5-61.T1 — posterior approaches empirical with sample
    long_post, short_post = make_priors()
    print(f"[posterior] priors: long={long_post.hit_rate:.4f} short={short_post.hit_rate:.4f}")

    for _ in range(100):
        long_post.update(won=True)
        short_post.update(won=False)
    print(f"[posterior] after 100 long-wins / 100 short-losses:")
    print(f"  long  → {long_post.hit_rate:.4f}  (should converge near 1.0)")
    print(f"  short → {short_post.hit_rate:.4f}  (should converge near 0.0)")
    assert long_post.hit_rate > 0.9, "long posterior should converge near 1.0"
    assert short_post.hit_rate < 0.1, "short posterior should converge near 0.0"

    # Divergence check (S5-61.T2 — directions independent)
    div = long_post.divergence_from(short_post)
    print(f"[posterior] divergence = {div*100:.1f}pp")
    msg = check_divergence(long_post, short_post, direction_divergence_pp=15.0)
    print(f"[posterior] divergence alert: {msg}")
    assert msg is not None, "diverged posteriors should fire alert"

    # Drift check
    long_post2, _ = make_priors()
    for _ in range(10):
        long_post2.update(won=False)  # 10 losses bring posterior down
    drift_msg = check_drift(long_post2, prior_hit_rate=0.55, drift_alert_pp=10.0)
    print(f"[posterior] drift alert (10 losses): {drift_msg}")

    print("[posterior] OK — Beta-Binomial per S5-61")
