"""
posterior_model.py — component (2) of scalp 2.

Bayesian update on top of scalp 1's GBM baseline. Per DESIGN.md §3b.

Contract:
  prior     = scalp 1 GBM(features)              # baseline AUC ~0.61
  likelihood_uw    = UW flow score at fire time  # +/- bumps logit
  likelihood_news  = news LLM score at fire time
  likelihood_gex   = GEX proximity score
  posterior = sigmoid(logit(prior) + λ_uw·L_uw + λ_news·L_news + λ_gex·L_gex)

λ_* fit by maximum likelihood on scalp 1's archive. Out-of-sample validated on 2026
holdout (the same split scalp 1's predictor uses).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class PosteriorScore:
    ts_utc: str
    ticker: str
    kind: str
    prior: float                   # scalp 1 GBM output, in [0, 1]
    posterior: float               # after Bayesian update, in [0, 1]
    likelihoods: dict[str, float]  # signed contributions per likelihood source
    features: dict                 # echo for audit


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
    """Apply Bayesian update in logit space.

    likelihoods: {source_name: signed_score}
    lambdas:     {source_name: weight} — fit by MLE in M2

    Returns posterior probability in [0, 1].
    """
    z = _logit(prior)
    for src, like in likelihoods.items():
        lam = lambdas.get(src, 0.0)
        z += lam * like
    return _sigmoid(z)


# ──────────────────────────────────────────────────────────────────────────────
# Stubs — to implement in M2
# ──────────────────────────────────────────────────────────────────────────────


def fit_lambdas(archive_fires, target_field: str = "atr_t1_plus_hit") -> dict[str, float]:
    """Fit λ_uw, λ_news, λ_gex by max-likelihood on scalp 1 archive.

    TODO M2:
      - For each archive fire with non-null UW/news/GEX features at fire time:
        - prior = scalp 1 GBM score (loaded from per_signal JSON if available,
          else recomputed locally from features)
        - target = atr_t1_plus_hit
      - Maximize log-likelihood over (λ_uw, λ_news, λ_gex)
      - Return fitted dict
    """
    raise NotImplementedError("M2 deliverable")


def score(features: dict, lambdas: dict[str, float], gbm_prior_fn=None) -> PosteriorScore:
    """Score one live fire. gbm_prior_fn is the scalp 1 GBM (callable)."""
    raise NotImplementedError("M2 deliverable")


if __name__ == "__main__":
    # Sanity check the logit/sigmoid math
    import sys
    p = 0.5
    z = _logit(p)
    p2 = _sigmoid(z)
    print(f"[posterior_model] logit(0.5)={z:.4f} sigmoid back={p2:.4f}")

    bumped = update_posterior(prior=0.5, likelihoods={"uw": 0.5}, lambdas={"uw": 1.0})
    print(f"[posterior_model] update_posterior(0.5, +0.5 uw, λ=1.0) = {bumped:.4f}")
    assert bumped > 0.5, "positive likelihood with positive lambda should raise posterior"
    print("[posterior_model] M1 scaffold OK — math primitives work, M2 fit/score stubbed")
