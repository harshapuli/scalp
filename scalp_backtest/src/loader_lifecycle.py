"""
loader_lifecycle.py — read engine_v4 per-ticker lifecycle snapshots.

READ-ONLY. Source:
  engine_v4/data/intraday/<DATE>/lifecycle/<TICKER>_<HHMMSS>.json

Each file is a 30-second snapshot of the ticker's full state:
  - tape (RVOL, day_pct, HOD/LOD, coil range)
  - conviction state
  - whale flow
  - news today
  - open_trade (kind, entry, state, P&L, exit reason)
  - trade_progression (price path, distance to targets, age)

We collapse the per-tick stream into a single LifecycleRecord per ticker:
  - first_seen_utc / last_seen_utc / n_snapshots
  - final_state (latest snapshot's state)
  - all_kinds_fired (every distinct kind that opened a trade for this ticker)
  - day_pnl_pct + final exit_reason
  - peak/trough underlying %
  - had_news / had_flow flags
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ENGINE_V4_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_v4")
INTRADAY_ROOT = ENGINE_V4_ROOT / "data" / "intraday"


@dataclass
class LifecycleRecord:
    ticker: str
    date_str: str
    n_snapshots: int = 0

    first_seen_utc: Optional[str] = None
    last_seen_utc: Optional[str] = None

    # mode/state from latest snapshot
    final_mode: Optional[str] = None              # "TRADE" | "WATCH" | etc.
    final_auto_mode: Optional[str] = None
    final_state: Optional[str] = None             # OPEN | EXITED | None

    # tape — latest
    last_close: Optional[float] = None
    day_pct_final: Optional[float] = None
    day_pct_peak: Optional[float] = None
    day_pct_trough: Optional[float] = None
    rvol_final: Optional[float] = None
    rvol_peak: Optional[float] = None
    hod: Optional[float] = None
    lod: Optional[float] = None

    # signals fired
    kinds_fired: list[str] = field(default_factory=list)   # distinct kinds that opened a trade
    n_kinds_fired: int = 0

    # open_trade summary (latest, if any)
    has_open_trade: bool = False
    open_trade_kind: Optional[str] = None
    open_trade_entry: Optional[float] = None
    open_trade_pnl_pct: Optional[float] = None       # last_underlying_pct from latest snapshot
    open_trade_best_pct: Optional[float] = None
    open_trade_worst_pct: Optional[float] = None
    open_trade_state: Optional[str] = None
    open_trade_exit_reason: Optional[str] = None

    # progression — latest
    age_min: Optional[float] = None
    time_left_min: Optional[float] = None
    side: Optional[str] = None                        # CALL/PUT/PRE_PUT_COIL/etc.
    best_directional_pct: Optional[float] = None
    worst_directional_pct: Optional[float] = None

    # context flags
    had_news: bool = False
    had_flow: bool = False
    had_conviction: bool = False

    # full price path from latest snapshot (last 30 bars typically)
    price_path_n: int = 0


def list_available_dates() -> list[str]:
    if not INTRADAY_ROOT.exists():
        return []
    out = []
    for p in sorted(INTRADAY_ROOT.iterdir()):
        if not p.is_dir():
            continue
        if (p / "lifecycle").exists():
            out.append(p.name)
    return out


def _parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _safe_get(d: Optional[dict], key: str, default=None):
    if not d:
        return default
    v = d.get(key, default)
    return v if v is not None else default


def load_lifecycle_for_date(date: str) -> list[LifecycleRecord]:
    """Group lifecycle/*.json by ticker, collapse into one LifecycleRecord per ticker."""
    lc_dir = INTRADAY_ROOT / date / "lifecycle"
    if not lc_dir.exists():
        return []

    by_ticker: dict[str, list[Path]] = {}
    for fp in lc_dir.iterdir():
        if fp.suffix != ".json":
            continue
        # filenames are TICKER_HHMMSS.json
        stem = fp.stem
        if "_" not in stem:
            continue
        ticker, _, _ = stem.rpartition("_")
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append(fp)

    out: list[LifecycleRecord] = []
    for ticker, files in by_ticker.items():
        files.sort()   # alphabetical = chronological for HHMMSS suffix
        rec = LifecycleRecord(ticker=ticker, date_str=date, n_snapshots=len(files))

        peak_pct = None
        trough_pct = None
        rvol_peak = None
        kinds_seen: set[str] = set()
        had_news = False
        had_flow = False
        had_conviction = False

        first_seen = None
        last_data: Optional[dict] = None

        # Read first + scan all (lightweight pass — we keep most context from latest)
        for fp in files:
            try:
                d = json.loads(fp.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            ts = d.get("as_of_utc")
            if first_seen is None and ts:
                first_seen = ts

            tape = d.get("tape") or {}
            day_pct = tape.get("day_pct")
            if day_pct is not None:
                if peak_pct is None or day_pct > peak_pct:
                    peak_pct = day_pct
                if trough_pct is None or day_pct < trough_pct:
                    trough_pct = day_pct
            rvol = tape.get("rvol")
            if rvol is not None and (rvol_peak is None or rvol > rvol_peak):
                rvol_peak = rvol

            # Track all kinds that opened a trade
            ot = d.get("open_trade") or {}
            kind = ot.get("kind") if ot else None
            if kind:
                kinds_seen.add(kind)

            news = d.get("news_today") or {}
            if news.get("has_news"):
                had_news = True
            whale = d.get("whale") or {}
            if whale.get("has_flow"):
                had_flow = True
            if d.get("conviction"):
                had_conviction = True

            last_data = d

        if not last_data:
            continue

        rec.first_seen_utc = first_seen
        rec.last_seen_utc = last_data.get("as_of_utc")
        rec.final_mode = last_data.get("mode")
        rec.final_auto_mode = last_data.get("auto_mode")

        tape = last_data.get("tape") or {}
        rec.last_close = tape.get("last_close")
        rec.day_pct_final = tape.get("day_pct")
        rec.day_pct_peak = peak_pct
        rec.day_pct_trough = trough_pct
        rec.rvol_final = tape.get("rvol")
        rec.rvol_peak = rvol_peak
        rec.hod = tape.get("hod")
        rec.lod = tape.get("lod")

        ot = last_data.get("open_trade") or {}
        if ot:
            rec.has_open_trade = True
            rec.open_trade_kind = ot.get("kind")
            rec.open_trade_entry = ot.get("entry_price")
            rec.open_trade_pnl_pct = ot.get("last_underlying_pct")
            rec.open_trade_best_pct = ot.get("best_option_pnl_pct")
            rec.open_trade_worst_pct = ot.get("worst_option_pnl_pct")
            rec.open_trade_state = ot.get("state")
            rec.open_trade_exit_reason = ot.get("exit_reason")
            rec.final_state = ot.get("state")

        prog = last_data.get("trade_progression") or {}
        if prog and prog.get("has_progression"):
            rec.side = prog.get("side")
            rec.age_min = prog.get("age_min")
            rec.time_left_min = prog.get("time_left_min")
            rec.best_directional_pct = prog.get("best_directional_pct")
            rec.worst_directional_pct = prog.get("worst_directional_pct")
            pp = prog.get("price_path") or []
            rec.price_path_n = len(pp)

        rec.kinds_fired = sorted(kinds_seen)
        rec.n_kinds_fired = len(kinds_seen)
        rec.had_news = had_news
        rec.had_flow = had_flow
        rec.had_conviction = had_conviction

        out.append(rec)

    out.sort(key=lambda r: (-r.n_snapshots, r.ticker))
    return out


def load_all_lifecycles(dates: Optional[list[str]] = None) -> list[LifecycleRecord]:
    if dates is None:
        dates = list_available_dates()
    out: list[LifecycleRecord] = []
    for d in dates:
        out.extend(load_lifecycle_for_date(d))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import time
    dates = list_available_dates()
    print(f"available lifecycle dates: {dates}")
    t0 = time.time()
    recs = load_all_lifecycles()
    dt = time.time() - t0
    print(f"loaded {len(recs)} ticker-lifecycles in {dt:.2f}s")

    # Top 10 by snapshot count
    print(f"\n{'TICKER':6s} {'snaps':>6s} {'mode':>6s} {'kind_fired':>20s} {'day%':>7s} {'pnl%':>7s} {'exit':>22s}")
    print("─" * 90)
    for r in recs[:15]:
        kinds = ",".join(r.kinds_fired) if r.kinds_fired else "—"
        if len(kinds) > 20:
            kinds = kinds[:17] + "..."
        dpf = f"{r.day_pct_final:+.2f}" if r.day_pct_final is not None else "—"
        pnl = f"{r.open_trade_pnl_pct:+.2f}" if r.open_trade_pnl_pct is not None else "—"
        exit_r = r.open_trade_exit_reason or r.final_state or "—"
        print(f"{r.ticker:6s} {r.n_snapshots:>6d} {(r.final_mode or '—'):>6s} {kinds:>20s} {dpf:>7s} {pnl:>7s} {exit_r:>22s}")

    # Stats
    n_trade_mode = sum(1 for r in recs if r.final_mode == "TRADE")
    n_with_open = sum(1 for r in recs if r.has_open_trade)
    n_news = sum(1 for r in recs if r.had_news)
    n_flow = sum(1 for r in recs if r.had_flow)
    print(f"\nTotal: {len(recs)} | TRADE-mode: {n_trade_mode} | with open_trade: {n_with_open} | had_news: {n_news} | had_flow: {n_flow}")
