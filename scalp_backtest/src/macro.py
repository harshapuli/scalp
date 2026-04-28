"""
macro.py — VIX bucket and SPX regime lookups, deps-free stub.

Per DESIGN.md §14 Q17 default: NO new deps allowed in v1. Functions return
None for vix_bucket and spx_regime, so downstream slicers/UI can branch on
"macro unknown" without crashing. Replace this module's implementation when
yfinance/IB feed is approved.

Public API:
    compute_macro(date_str) -> (vix_bucket, spx_regime, near_miss_eligible)
        vix_bucket : "low" | "med" | "high" | None
        spx_regime : "trend_up" | "chop" | "trend_down" | None
        near_miss_eligible : bool (True if VIX > some threshold; None when unknown)

Buckets reserved for later (so downstream code can rely on these names):
    VIX_LOW_HI = 15.0   # vix <= 15  → "low"
    VIX_MED_HI = 22.0   # 15 < vix <= 22 → "med"; vix > 22 → "high"
    NEAR_MISS_VIX_HI = 22.0
"""
from __future__ import annotations

from typing import Optional


VIX_LOW_HI = 15.0
VIX_MED_HI = 22.0
NEAR_MISS_VIX_HI = 22.0


def vix_bucket_from_value(vix: Optional[float]) -> Optional[str]:
    if vix is None:
        return None
    if vix <= VIX_LOW_HI:
        return "low"
    if vix <= VIX_MED_HI:
        return "med"
    return "high"


def spx_regime_from_returns(ret_5d_pct: Optional[float],
                            ret_20d_pct: Optional[float]) -> Optional[str]:
    """Crude regime tag — replace with proper trend filter when SPX bars are available."""
    if ret_5d_pct is None or ret_20d_pct is None:
        return None
    if ret_5d_pct > 1.0 and ret_20d_pct > 2.0:
        return "trend_up"
    if ret_5d_pct < -1.0 and ret_20d_pct < -2.0:
        return "trend_down"
    return "chop"


def compute_macro(date_str: str) -> tuple[Optional[str], Optional[str], Optional[bool]]:
    """
    Stub — returns (None, None, None). When deps are wired:
      - fetch VIX close on date_str → bucket
      - fetch SPX bars → 5d/20d returns → regime
      - near_miss_eligible = vix > NEAR_MISS_VIX_HI
    """
    return (None, None, None)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("macro.py — deps-free stub (DESIGN.md §14 Q17)")
    print(f"  vix_bucket('2026-04-25') = {compute_macro('2026-04-25')[0]}")
    print(f"  spx_regime('2026-04-25') = {compute_macro('2026-04-25')[1]}")
    print("  → all None until yfinance/IB feed wired")
