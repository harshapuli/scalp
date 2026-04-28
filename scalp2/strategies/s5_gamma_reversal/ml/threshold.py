"""
strategies/s5_gamma_reversal/ml/threshold.py — EV-curve threshold picker (S5-35).

Per spec §10.5 (pseudocode `pick_threshold`) and §10.4 s5.ml. Iterates
threshold in [0.50, 0.85] step 0.01. Computes net EV using cost model.
Rejects extremes ≤ 0.51 or ≥ 0.83 with warning. Min 30 validation trades
per threshold; prefer 50+ for production.

Two callable surfaces:
  - pick_threshold(probabilities, labels, costs, payoff, cfg) → (best_thr, best_ev)
    [implemented in M3 — currently stub]
  - ev_lookup(curve, posterior_pct, key) — linear interpolation between
    fitted EV curve anchors [implemented now, from inferred scaffold]

NOTE: replaces inferred scaffold formerly at scalp2/src/ev_curve.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Module is at strategies/s5_gamma_reversal/ml/threshold.py
# Project root is 4 dirs up
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EV_CURVES_DIR = PROJECT_ROOT / "data" / "ev_curves"


@dataclass
class EVCurvePoint:
    percentile: float       # 0–100, the posterior percentile bucket midpoint
    ev_mean: float          # mean realized pnl% in this bucket
    ev_p25: float
    ev_p75: float
    n: int


@dataclass
class EVCurve:
    kind: str
    fitted_at_utc: str
    n_archive_fires: int
    points: list[EVCurvePoint]


# Default percentile bucket midpoints (10 deciles)
DEFAULT_PERCENTILES = [5, 15, 25, 35, 45, 55, 65, 75, 85, 95]


# ──────────────────────────────────────────────────────────────────────────────
# EV-curve fitting (S5-35) + threshold picker (spec §10.5 pick_threshold)
# ──────────────────────────────────────────────────────────────────────────────


import statistics


def fit_curve(kind: str,
              archive_fires_with_posteriors: list[tuple[float, float]],
              percentiles: Optional[list[int]] = None) -> EVCurve:
    """Fit an EV curve from (posterior, realized_pnl_pct) pairs.

    Sort by posterior, bin into N percentile windows, compute ev_mean,
    ev_p25, ev_p75 per bin.
    """
    if not archive_fires_with_posteriors:
        return EVCurve(
            kind=kind,
            fitted_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            n_archive_fires=0,
            points=[],
        )

    sorted_pairs = sorted(archive_fires_with_posteriors, key=lambda p: p[0])
    pcts = percentiles or DEFAULT_PERCENTILES
    n = len(sorted_pairs)
    # Each percentile p in [0, 100] maps to a bucket centered on that percentile;
    # the bucket size is total_n / n_buckets
    bucket_size = max(1, n // len(pcts))
    points: list[EVCurvePoint] = []
    for i, p in enumerate(pcts):
        lo = i * bucket_size
        hi = (i + 1) * bucket_size if i < len(pcts) - 1 else n
        bucket = sorted_pairs[lo:hi]
        if not bucket:
            continue
        pnls = [pair[1] for pair in bucket]
        try:
            p25 = statistics.quantiles(pnls, n=4)[0] if len(pnls) >= 4 else min(pnls)
            p75 = statistics.quantiles(pnls, n=4)[2] if len(pnls) >= 4 else max(pnls)
        except statistics.StatisticsError:
            p25, p75 = min(pnls), max(pnls)
        points.append(EVCurvePoint(
            percentile=float(p),
            ev_mean=float(statistics.mean(pnls)),
            ev_p25=float(p25),
            ev_p75=float(p75),
            n=len(bucket),
        ))

    return EVCurve(
        kind=kind,
        fitted_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        n_archive_fires=n,
        points=points,
    )


def pick_threshold(probabilities,
                    labels,
                    costs_per_trade: float,
                    payoff_per_winner: float,
                    threshold_low: float = 0.50,
                    threshold_high: float = 0.85,
                    threshold_extreme_warn_low: float = 0.51,
                    threshold_extreme_warn_high: float = 0.83,
                    min_validation_trades: int = 30) -> tuple[float, float, list[str]]:
    """Spec §10.5 pick_threshold pseudocode.

    Returns (best_thr, best_ev_per_trade, warnings).

    Iterates threshold in [threshold_low, threshold_high] step 0.01.
    Skips thresholds where traded count < min_validation_trades.
    Warns if best threshold is at the extreme (≤0.51 or ≥0.83).
    """
    import numpy as np
    probs = np.asarray(probabilities)
    lbls = np.asarray(labels)

    best_thr = threshold_low
    best_ev = -float("inf")
    warnings: list[str] = []

    thr_arr = np.arange(threshold_low, threshold_high + 1e-9, 0.01)
    for thr in thr_arr:
        traded = probs >= thr
        n_traded = int(traded.sum())
        if n_traded < min_validation_trades:
            continue
        wins = int((traded & (lbls == 1)).sum())
        losses = int((traded & (lbls == 0)).sum())
        ev = (wins * payoff_per_winner
              - losses * costs_per_trade  # losses cost full cost
              - n_traded * costs_per_trade)
        ev_per_trade = ev / n_traded
        if ev_per_trade > best_ev:
            best_thr, best_ev = float(thr), float(ev_per_trade)

    if best_thr <= threshold_extreme_warn_low:
        warnings.append(
            f"threshold {best_thr:.2f} at lower extreme ≤ {threshold_extreme_warn_low}; "
            "investigate calibration"
        )
    if best_thr >= threshold_extreme_warn_high:
        warnings.append(
            f"threshold {best_thr:.2f} at upper extreme ≥ {threshold_extreme_warn_high}; "
            "investigate calibration"
        )
    return best_thr, best_ev, warnings


def save_curve(curve: EVCurve) -> Path:
    """Persist a fitted curve to data/ev_curves/<KIND>.json."""
    EV_CURVES_DIR.mkdir(parents=True, exist_ok=True)
    p = EV_CURVES_DIR / f"{curve.kind}.json"
    p.write_text(json.dumps({
        "kind": curve.kind,
        "fitted_at_utc": curve.fitted_at_utc,
        "n_archive_fires": curve.n_archive_fires,
        "curve": [
            {"percentile": pt.percentile, "ev_mean": pt.ev_mean,
             "ev_p25": pt.ev_p25, "ev_p75": pt.ev_p75, "n": pt.n}
            for pt in curve.points
        ],
    }, indent=2))
    return p


def load_curve(kind: str) -> Optional[EVCurve]:
    p = EV_CURVES_DIR / f"{kind}.json"
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return EVCurve(
        kind=raw["kind"],
        fitted_at_utc=raw["fitted_at_utc"],
        n_archive_fires=raw["n_archive_fires"],
        points=[EVCurvePoint(**pt) for pt in raw["curve"]],
    )


def ev_lookup(curve: EVCurve, posterior_pct: float, key: str = "ev_mean") -> Optional[float]:
    """Linear interpolation between the two nearest curve points.

    posterior_pct in [0, 100]. Returns ev_mean / ev_p25 / ev_p75 from `key`.
    """
    if not curve.points:
        return None
    pts = sorted(curve.points, key=lambda p: p.percentile)
    if posterior_pct <= pts[0].percentile:
        return getattr(pts[0], key)
    if posterior_pct >= pts[-1].percentile:
        return getattr(pts[-1], key)
    for lo, hi in zip(pts, pts[1:]):
        if lo.percentile <= posterior_pct <= hi.percentile:
            span = hi.percentile - lo.percentile
            if span <= 0:
                return getattr(lo, key)
            t = (posterior_pct - lo.percentile) / span
            return getattr(lo, key) + t * (getattr(hi, key) - getattr(lo, key))
    return getattr(pts[-1], key)


if __name__ == "__main__":
    # Quick sanity on the lookup math with a synthetic curve
    pts = [EVCurvePoint(percentile=p, ev_mean=p / 100.0 - 0.5,
                         ev_p25=p / 100.0 - 0.7, ev_p75=p / 100.0 - 0.3, n=10)
           for p in DEFAULT_PERCENTILES]
    c = EVCurve(kind="SYNTH", fitted_at_utc="now", n_archive_fires=100, points=pts)
    print(f"[ev_curve] synth curve at p10 ev_mean = {ev_lookup(c, 10):.4f}")
    print(f"[ev_curve] synth curve at p50 ev_mean = {ev_lookup(c, 50):.4f}")
    print(f"[ev_curve] synth curve at p90 ev_mean = {ev_lookup(c, 90):.4f}")
    print("[ev_curve] M1 scaffold OK — lookup interpolation works, M3 fit/save/load stubbed")
