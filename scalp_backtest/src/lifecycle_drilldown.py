"""
lifecycle_drilldown.py — per-ticker timeline from engine_v4 lifecycle snapshots.

Heavier than loader_lifecycle.py (which collapses to one record per ticker).
This reads ALL snapshot files for a (ticker, date) and returns the full
chronological timeline plus deduped news / flow events for drilldown rendering.

READ-ONLY on engine_v4/data/intraday/<DATE>/lifecycle/<TICKER>_<HHMMSS>.json.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


ENGINE_V4_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_v4")
INTRADAY_ROOT = ENGINE_V4_ROOT / "data" / "intraday"


@dataclass
class TimelinePoint:
    as_of_utc: Optional[str] = None
    mode: Optional[str] = None
    auto_mode: Optional[str] = None
    last_close: Optional[float] = None
    day_pct: Optional[float] = None
    rvol: Optional[float] = None
    hod: Optional[float] = None
    lod: Optional[float] = None
    coil_range_pct: Optional[float] = None
    open_trade_kind: Optional[str] = None
    open_trade_state: Optional[str] = None
    open_trade_pnl_pct: Optional[float] = None
    open_trade_best_pct: Optional[float] = None
    open_trade_worst_pct: Optional[float] = None
    open_trade_exit_reason: Optional[str] = None
    has_news: bool = False
    has_flow: bool = False
    has_conviction: bool = False


@dataclass
class NewsItem:
    headline: Optional[str] = None
    source: Optional[str] = None
    sentiment: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class FlowItem:
    type_: Optional[str] = None       # "call" / "put"
    strike: Optional[str] = None
    expiry: Optional[str] = None
    premium_m: Optional[float] = None
    rule: Optional[str] = None
    is_sweep: bool = False
    created_at: Optional[str] = None


@dataclass
class FlowAggSnapshot:
    """Snapshot of whale aggregates from the latest snap."""
    n_alerts: Optional[int] = None
    sweep_n: Optional[int] = None
    call_prem_m: Optional[float] = None
    put_prem_m: Optional[float] = None
    net_call_skew_m: Optional[float] = None
    call_share: Optional[float] = None
    put_share: Optional[float] = None


@dataclass
class TickerTimeline:
    ticker: str
    date_str: str
    n_snapshots: int = 0
    points: list[TimelinePoint] = field(default_factory=list)
    news_seen: list[NewsItem] = field(default_factory=list)
    flow_seen: list[FlowItem] = field(default_factory=list)
    final_open_trade: Optional[dict] = None
    final_progression: Optional[dict] = None
    final_flow_agg: Optional[FlowAggSnapshot] = None
    kinds_fired_chronological: list[tuple[str, str]] = field(default_factory=list)


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_timeline(ticker: str, date_str: str) -> Optional[TickerTimeline]:
    """Load full snapshot timeline for (ticker, date). None if no files."""
    lc_dir = INTRADAY_ROOT / date_str / "lifecycle"
    if not lc_dir.exists():
        return None
    files = sorted(lc_dir.glob(f"{ticker}_*.json"))
    if not files:
        return None

    tl = TickerTimeline(ticker=ticker, date_str=date_str, n_snapshots=len(files))
    seen_kinds: set[str] = set()
    seen_news_keys: set[str] = set()
    seen_flow_keys: set[str] = set()

    # Filename HHMMSS is unreliable (sometimes PT, sometimes UTC, sometimes
    # shifted). Sort by `as_of_utc` field within each file for true chronology.
    raw_records: list[dict] = []
    for fp in files:
        try:
            d = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        raw_records.append(d)
    raw_records.sort(key=lambda d: d.get("as_of_utc") or "")
    tl.n_snapshots = len(raw_records)

    last_data: Optional[dict] = None
    for d in raw_records:
        last_data = d

        tape = d.get("tape") or {}
        ot = d.get("open_trade") or {}
        news = d.get("news_today") or {}
        whale = d.get("whale") or {}

        pt = TimelinePoint(
            as_of_utc=d.get("as_of_utc"),
            mode=d.get("mode"),
            auto_mode=d.get("auto_mode"),
            last_close=_safe_float(tape.get("last_close")),
            day_pct=_safe_float(tape.get("day_pct")),
            rvol=_safe_float(tape.get("rvol")),
            hod=_safe_float(tape.get("hod")),
            lod=_safe_float(tape.get("lod")),
            coil_range_pct=_safe_float(tape.get("coil_range_pct")),
            open_trade_kind=ot.get("kind"),
            open_trade_state=ot.get("state"),
            open_trade_pnl_pct=_safe_float(ot.get("last_underlying_pct")),
            open_trade_best_pct=_safe_float(ot.get("best_option_pnl_pct")),
            open_trade_worst_pct=_safe_float(ot.get("worst_option_pnl_pct")),
            open_trade_exit_reason=ot.get("exit_reason"),
            has_news=bool(news.get("has_news")),
            has_flow=bool(whale.get("has_flow")),
            has_conviction=bool(d.get("conviction")),
        )
        tl.points.append(pt)

        # Chronological kinds (first appearance only)
        kind = ot.get("kind")
        if kind and kind not in seen_kinds:
            seen_kinds.add(kind)
            tl.kinds_fired_chronological.append((d.get("as_of_utc") or "", kind))

        # News (dedup by headline) — engine_v4 uses news_today.recent[]
        for ni in (news.get("recent") or []):
            headline = ni.get("headline") or ni.get("title") or ""
            if headline and headline not in seen_news_keys:
                seen_news_keys.add(headline)
                tl.news_seen.append(NewsItem(
                    headline=headline,
                    source=ni.get("source"),
                    sentiment=ni.get("sentiment") or ni.get("polarity"),
                    created_at=ni.get("created_at"),
                ))

        # Flow (dedup by type+strike+expiry+premium) — engine_v4 uses whale.top3[]
        for f in (whale.get("top3") or []):
            type_ = f.get("type") or ""
            strike = f.get("strike") or ""
            expiry = f.get("expiry") or ""
            premium = _safe_float(f.get("premium_m"))
            key = f"{type_}-{strike}-{expiry}-{premium}"
            if type_ and key not in seen_flow_keys:
                seen_flow_keys.add(key)
                tl.flow_seen.append(FlowItem(
                    type_=type_,
                    strike=strike,
                    expiry=expiry,
                    premium_m=premium,
                    rule=f.get("rule"),
                    is_sweep=bool(f.get("is_sweep")),
                    created_at=f.get("created_at"),
                ))

    if last_data:
        tl.final_open_trade = last_data.get("open_trade") or None
        tl.final_progression = last_data.get("trade_progression") or None
        wh = last_data.get("whale") or {}
        if wh.get("has_flow"):
            tl.final_flow_agg = FlowAggSnapshot(
                n_alerts=wh.get("n_alerts"),
                sweep_n=wh.get("sweep_n"),
                call_prem_m=_safe_float(wh.get("call_prem_m")),
                put_prem_m=_safe_float(wh.get("put_prem_m")),
                net_call_skew_m=_safe_float(wh.get("net_call_skew_m")),
                call_share=_safe_float(wh.get("call_share")),
                put_share=_safe_float(wh.get("put_share")),
            )

    return tl


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    import time
    if len(sys.argv) < 3:
        # Use most recent date and an interesting ticker
        from loader_lifecycle import list_available_dates
        dates = list_available_dates()
        if not dates:
            print("no dates available")
            sys.exit(1)
        date = dates[-1]
        ticker = "NVDA"
    else:
        ticker, date = sys.argv[1], sys.argv[2]

    t0 = time.time()
    tl = load_timeline(ticker, date)
    dt = time.time() - t0
    if not tl:
        print(f"no timeline for {ticker} on {date}")
        sys.exit(1)
    print(f"loaded {tl.n_snapshots} snapshots for {ticker} {date} in {dt:.2f}s")
    print(f"  kinds fired chronologically: {tl.kinds_fired_chronological}")
    print(f"  news items: {len(tl.news_seen)}")
    print(f"  flow events: {len(tl.flow_seen)}")
    if tl.points:
        first = tl.points[0]
        last = tl.points[-1]
        print(f"  first @ {first.as_of_utc}  mode={first.mode}  day%={first.day_pct}")
        print(f"  last  @ {last.as_of_utc}  mode={last.mode}  day%={last.day_pct}")
