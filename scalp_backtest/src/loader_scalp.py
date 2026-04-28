"""
loader_scalp.py — read every engine_v4 scalp signal into Signal records.

READ-ONLY on engine_v4. Sources:
  data/intraday/<DATE>/breakouts_validated.jsonl   ← validated bucket
  data/intraday/<DATE>/breakouts_coil.jsonl        ← coil/ramp bucket
  data/intraday/<DATE>/bars/<TICKER>.jsonl         ← 1-min OHLCV

Outputs Signal + Bar dataclasses; later cached to pickle by pipeline.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ENGINE_V4_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_v4")
INTRADAY_ROOT = ENGINE_V4_ROOT / "data" / "intraday"


# ──────────────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Signal:
    # identity
    source: str               # "validated" | "coil"
    kind: str
    side: str                 # CALL | PUT
    ticker: str
    detected_utc: str
    detected_epoch: float
    detected_minute_utc: str
    date_str: str             # "YYYY-MM-DD" — bucket key
    entry_price: float

    # context (varies by source/kind)
    confidence: Optional[float] = None
    rvol: Optional[float] = None
    day_pct: Optional[float] = None
    coil_range_pct: Optional[float] = None
    dist_above_lod_pct: Optional[float] = None
    why: Optional[str] = None

    # engine's own bookkeeping (for cross-reference)
    recorded_state: Optional[str] = None
    recorded_exit_reason: Optional[str] = None
    recorded_exit_price: Optional[float] = None
    recorded_option_pnl_pct: Optional[float] = None
    recorded_best_option_pnl_pct: Optional[float] = None
    recorded_worst_option_pnl_pct: Optional[float] = None
    recorded_last_underlying_pct: Optional[float] = None
    recorded_hit_5m: Optional[bool] = None
    recorded_hit_15m: Optional[bool] = None
    recorded_hit_30m: Optional[bool] = None

    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Bar:
    t: str
    epoch: float
    o: float
    h: float
    l: float
    c: float
    v: int
    vw: float


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _to_epoch(iso: str) -> float:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _floor_to_minute(iso: str) -> str:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s).astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def _date_key(iso: str) -> str:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────────────────
# Per-source loaders
# ──────────────────────────────────────────────────────────────────────────────


def load_validated(date: str) -> list[Signal]:
    path = INTRADAY_ROOT / date / "breakouts_validated.jsonl"
    out: list[Signal] = []
    if not path.exists():
        return out

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            extra = d.get("extra", {}) or {}
            detected = d.get("detected_utc") or d.get("opened_utc")
            if not detected:
                continue
            ep = _to_epoch(detected)
            sig = Signal(
                source="validated",
                kind=d.get("kind") or "?",
                side=d.get("side") or ("CALL" if "CALL" in (d.get("kind") or "") else "PUT"),
                ticker=d.get("ticker", "?"),
                detected_utc=detected,
                detected_epoch=ep,
                detected_minute_utc=_floor_to_minute(detected),
                date_str=_date_key(detected),
                entry_price=float(d.get("entry_price") or 0.0),
                confidence=extra.get("confidence"),
                why=extra.get("status"),
                recorded_state=d.get("state"),
                recorded_exit_reason=d.get("exit_reason"),
                recorded_exit_price=d.get("exit_price"),
                recorded_option_pnl_pct=d.get("option_pnl_pct"),
                recorded_best_option_pnl_pct=d.get("best_option_pnl_pct"),
                recorded_worst_option_pnl_pct=d.get("worst_option_pnl_pct"),
                recorded_last_underlying_pct=d.get("last_underlying_pct"),
                recorded_hit_5m=d.get("hit_5m"),
                recorded_hit_15m=d.get("hit_15m"),
                recorded_hit_30m=d.get("hit_30m"),
                raw=d,
            )
            out.append(sig)
    return out


def load_coil(date: str) -> list[Signal]:
    path = INTRADAY_ROOT / date / "breakouts_coil.jsonl"
    out: list[Signal] = []
    if not path.exists():
        return out

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            kind = d.get("kind") or "?"
            features = d.get("features", {}) or {}
            detected = d.get("detected_utc") or d.get("opened_utc")
            if not detected:
                continue
            ep = _to_epoch(detected)
            side = "CALL" if "CALL" in kind else "PUT"
            sig = Signal(
                source="coil",
                kind=kind,
                side=side,
                ticker=d.get("ticker", "?"),
                detected_utc=detected,
                detected_epoch=ep,
                detected_minute_utc=_floor_to_minute(detected),
                date_str=_date_key(detected),
                entry_price=float(d.get("entry_price") or 0.0),
                rvol=d.get("rvol_at_entry") or features.get("rvol"),
                day_pct=d.get("day_pct_at_entry") or features.get("day_pct"),
                coil_range_pct=features.get("coil_range_pct"),
                dist_above_lod_pct=features.get("dist_above_lod_pct"),
                why=d.get("why"),
                recorded_state=d.get("state"),
                recorded_exit_reason=d.get("exit_reason"),
                recorded_exit_price=d.get("exit_price"),
                recorded_option_pnl_pct=d.get("option_pnl_pct"),
                recorded_best_option_pnl_pct=d.get("best_option_pnl_pct"),
                recorded_worst_option_pnl_pct=d.get("worst_option_pnl_pct"),
                recorded_last_underlying_pct=d.get("last_underlying_pct"),
                recorded_hit_5m=d.get("hit_5m"),
                recorded_hit_15m=d.get("hit_15m"),
                recorded_hit_30m=d.get("hit_30m"),
                raw=d,
            )
            out.append(sig)
    return out


def load_bars(date: str) -> dict[str, list[Bar]]:
    bars_dir = INTRADAY_ROOT / date / "bars"
    out: dict[str, list[Bar]] = {}
    if not bars_dir.exists():
        return out

    for fp in sorted(bars_dir.iterdir()):
        if fp.suffix != ".jsonl":
            continue
        ticker = fp.stem
        rows: list[Bar] = []
        with fp.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("t")
                if not t:
                    continue
                rows.append(Bar(
                    t=t,
                    epoch=_to_epoch(t),
                    o=float(d["o"]),
                    h=float(d["h"]),
                    l=float(d["l"]),
                    c=float(d["c"]),
                    v=int(d.get("v") or 0),
                    vw=float(d.get("vw") or d["c"]),
                ))
        rows.sort(key=lambda b: b.epoch)
        out[ticker] = rows
    return out


def list_available_dates() -> list[str]:
    if not INTRADAY_ROOT.exists():
        return []
    dates = []
    for p in sorted(INTRADAY_ROOT.iterdir()):
        if not p.is_dir():
            continue
        if (p / "breakouts_validated.jsonl").exists() or (p / "breakouts_coil.jsonl").exists():
            dates.append(p.name)
    return dates


def load_all(dates: Optional[list[str]] = None) -> tuple[list[Signal], dict[tuple[str, str], list[Bar]]]:
    """
    Returns (signals, bars).
    bars is keyed by (date_str, ticker) so multi-date doesn't collide.
    """
    if dates is None:
        dates = list_available_dates()

    signals: list[Signal] = []
    bars: dict[tuple[str, str], list[Bar]] = {}

    for date in dates:
        signals.extend(load_validated(date))
        signals.extend(load_coil(date))
        for ticker, rows in load_bars(date).items():
            bars[(date, ticker)] = rows

    signals.sort(key=lambda s: (s.detected_epoch, s.source, s.ticker))
    return signals, bars


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    dates = list_available_dates()
    print(f"available dates: {dates}")
    sigs, bars = load_all()
    n_v = sum(1 for s in sigs if s.source == "validated")
    n_c = sum(1 for s in sigs if s.source == "coil")
    print(f"signals: {len(sigs)}  (validated={n_v}, coil={n_c})")
    print(f"ticker bar series: {len(bars)}")

    by_kind = {}
    for s in sigs:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    print("kinds:")
    for k in sorted(by_kind, key=lambda x: -by_kind[x]):
        print(f"  {k:20s} {by_kind[k]}")
