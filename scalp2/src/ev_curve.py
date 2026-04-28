"""
ev_curve.py — component (3) of scalp 2.

Per signal kind, fit an EV curve from scalp 1's archive: expected pnl% as a
function of *posterior score percentile*. Curves typically rise monotonically.

Per DESIGN.md §3c.

The scoring layer (component 4 — Kelly) consumes ev_lookup() at fire time:
  posterior_pct = percentile_rank(posterior, kind)
  ev_expected   = ev_lookup(kind, posterior_pct, "ev_mean")
  ev_p25        = ev_lookup(kind, posterior_pct, "ev_p25")

If ev_expected ≤ 0 → SKIP regardless of phase gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
# Stubs — to implement in M3
# ──────────────────────────────────────────────────────────────────────────────


def fit_curve(kind: str,
              archive_fires_with_posteriors: list[tuple[float, float]],
              percentiles: list[int] = None) -> EVCurve:
    """Fit an EV curve from (posterior, realized_pnl_pct) pairs.

    TODO M3:
      - Sort by posterior, bin into deciles (or `percentiles` arg)
      - Per bin: compute ev_mean, ev_p25, ev_p75, n
      - Return EVCurve
    """
    raise NotImplementedError("M3 deliverable")


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
