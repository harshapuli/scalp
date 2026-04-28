"""
fire_time_enrich.py — for each scalp Outcome, join the engine_v4 lifecycle
snapshot closest to fire time and extract context features.

These become ML features:
  - at_fire_has_whale_flow / call_share / net_call_skew_m / sweep_n
  - at_fire_has_news / news_count
  - at_fire_has_conviction
  - at_fire_rvol / day_pct / mode

READ-ONLY on engine_v4. Re-uses lifecycle_drilldown.load_timeline().
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from outcome_intraday import Outcome
from lifecycle_drilldown import load_timeline, INTRADAY_ROOT


def _iso_to_epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = str(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, AttributeError):
        return None


def _index_snapshots_for_ticker(ticker: str, date_str: str) -> list[tuple[float, dict]]:
    """Return [(epoch, raw_dict)] sorted by epoch. Reads raw snapshots, not
    the trimmed TimelinePoint dataclass — we need the full whale block."""
    lc_dir = INTRADAY_ROOT / date_str / "lifecycle"
    if not lc_dir.exists():
        return []
    files = sorted(lc_dir.glob(f"{ticker}_*.json"))
    if not files:
        return []
    out: list[tuple[float, dict]] = []
    for fp in files:
        try:
            d = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ep = _iso_to_epoch(d.get("as_of_utc"))
        if ep is not None:
            out.append((ep, d))
    out.sort(key=lambda x: x[0])
    return out


def _nearest_snapshot(idx: list[tuple[float, dict]], target_epoch: float) -> Optional[tuple[float, dict]]:
    """Linear-scan nearest by absolute epoch distance.
    With a few hundred snaps per ticker this is fast enough; a binary search
    isn't worth the complexity given the bounded size."""
    if not idx:
        return None
    best = idx[0]
    best_dt = abs(best[0] - target_epoch)
    for ep, d in idx[1:]:
        dt = abs(ep - target_epoch)
        if dt < best_dt:
            best = (ep, d)
            best_dt = dt
    return best


def enrich(outcomes: list[Outcome]) -> int:
    """Mutate each Outcome in place with at_fire_* features. Returns count enriched."""
    # Cache snapshot index per (ticker, date) to avoid re-reading
    cache: dict[tuple[str, str], list[tuple[float, dict]]] = {}
    n_enriched = 0

    for o in outcomes:
        key = (o.ticker, o.date_str)
        if key not in cache:
            cache[key] = _index_snapshots_for_ticker(o.ticker, o.date_str)
        idx = cache[key]
        if not idx:
            continue

        nearest = _nearest_snapshot(idx, o.detected_epoch)
        if not nearest:
            continue

        snap_ep, snap = nearest
        tape = snap.get("tape") or {}
        whale = snap.get("whale") or {}
        news = snap.get("news_today") or {}

        o.at_fire_mode = snap.get("mode")
        o.at_fire_rvol = tape.get("rvol")
        o.at_fire_day_pct = tape.get("day_pct")

        # whale
        has_flow = bool(whale.get("has_flow"))
        o.at_fire_has_whale_flow = has_flow
        if has_flow:
            o.at_fire_call_share = whale.get("call_share")
            o.at_fire_net_call_skew_m = whale.get("net_call_skew_m")
            o.at_fire_sweep_n = whale.get("sweep_n")
            o.at_fire_whale_n_alerts = whale.get("n_alerts")

        # news
        has_news = bool(news.get("has_news"))
        o.at_fire_has_news = has_news
        if has_news:
            o.at_fire_news_count = news.get("n")

        # conviction (presence of dict alone is the signal)
        o.at_fire_has_conviction = bool(snap.get("conviction"))

        o.at_fire_snapshot_lag_sec = snap_ep - o.detected_epoch

        n_enriched += 1

    return n_enriched


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import time
    from loader_scalp import load_all
    from outcome_intraday import compute_all, dedup_signals

    t0 = time.time()
    sigs, bars = load_all()
    dedup = dedup_signals(sigs)
    outs = compute_all(dedup, bars)
    print(f"loaded {len(outs)} outcomes in {time.time()-t0:.2f}s")

    t1 = time.time()
    n_enriched = enrich(outs)
    print(f"enriched {n_enriched}/{len(outs)} in {time.time()-t1:.2f}s")

    # Quick stats
    n_flow = sum(1 for o in outs if o.at_fire_has_whale_flow)
    n_news = sum(1 for o in outs if o.at_fire_has_news)
    n_conv = sum(1 for o in outs if o.at_fire_has_conviction)
    print(f"\nfire-time context:")
    print(f"  had whale flow: {n_flow} ({100*n_flow/len(outs):.1f}%)")
    print(f"  had news:       {n_news} ({100*n_news/len(outs):.1f}%)")
    print(f"  had conviction: {n_conv} ({100*n_conv/len(outs):.1f}%)")

    # Show a few examples
    print(f"\nfirst 5 outcomes with whale flow:")
    print(f"{'TICKER':6s} {'KIND':24s} {'side':5s} {'call%':>6s} {'net$M':>7s} {'sweep':>5s}")
    seen = 0
    for o in outs:
        if o.at_fire_has_whale_flow and seen < 5:
            print(f"{o.ticker:6s} {o.kind:24s} {o.side:5s} "
                  f"{(o.at_fire_call_share or 0)*100:>5.1f}% "
                  f"{o.at_fire_net_call_skew_m or 0:>+7.2f} "
                  f"{o.at_fire_sweep_n or 0:>5d}")
            seen += 1
