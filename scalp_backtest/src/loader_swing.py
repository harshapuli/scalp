"""
loader_swing.py — read every swing fire emitted by shadow_logger_swing into
Signal records.

Source: data/swing_log/<DATE>/swing_signals.jsonl   (this project, not engine)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWING_LOG_ROOT = PROJECT_ROOT / "data" / "swing_log"


@dataclass
class SwingSignal:
    source: str               # "swing_v2" | "swing_strategy"
    kind: str
    side: str                 # CALL | PUT
    ticker: str
    detected_utc: str
    detected_epoch: float
    detected_minute_utc: str
    date_str: str
    entry_premium: Optional[float] = None
    underlying_close: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    qty: Optional[int] = None
    occ_symbol: Optional[str] = None
    strike: Optional[float] = None
    expiration: Optional[str] = None
    option_type: Optional[str] = None
    target_strike: Optional[float] = None
    target_dte: Optional[str] = None
    risk_pct: Optional[float] = None
    compression_active: Optional[bool] = None
    mss_active: Optional[bool] = None
    streak_mins: Optional[int] = None
    news_sentiment: Optional[str] = None
    raw_signal: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)


def _floor_minute(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def _date_key(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def list_available_dates() -> list[str]:
    if not SWING_LOG_ROOT.exists():
        return []
    return sorted(p.name for p in SWING_LOG_ROOT.iterdir()
                  if p.is_dir() and (p / "swing_signals.jsonl").exists())


def load_swing(date: str) -> list[SwingSignal]:
    path = SWING_LOG_ROOT / date / "swing_signals.jsonl"
    out: list[SwingSignal] = []
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ep = float(d.get("detected_epoch") or 0.0)
            if not ep:
                # fall back to parsing iso
                try:
                    iso = d.get("detected_utc", "")
                    if iso.endswith("Z"):
                        iso = iso[:-1] + "+00:00"
                    ep = datetime.fromisoformat(iso).timestamp()
                except Exception:
                    continue
            out.append(SwingSignal(
                source=d.get("source", "swing_v2"),
                kind=d.get("kind", "?"),
                side=d.get("side", "?"),
                ticker=d.get("ticker", "?"),
                detected_utc=d.get("detected_utc", ""),
                detected_epoch=ep,
                detected_minute_utc=_floor_minute(ep),
                date_str=_date_key(ep),
                entry_premium=d.get("entry_premium"),
                underlying_close=d.get("underlying_close"),
                stop_loss=d.get("stop_loss"),
                take_profit=d.get("take_profit"),
                qty=d.get("qty"),
                occ_symbol=d.get("occ_symbol"),
                strike=d.get("strike"),
                expiration=d.get("expiration"),
                option_type=d.get("option_type"),
                target_strike=d.get("target_strike"),
                target_dte=d.get("target_dte"),
                risk_pct=d.get("risk_pct"),
                compression_active=d.get("compression_active"),
                mss_active=d.get("mss_active"),
                streak_mins=d.get("streak_mins"),
                news_sentiment=d.get("news_sentiment"),
                raw_signal=d.get("raw_signal"),
                raw=d.get("raw", {}),
            ))
    return out


def load_all(dates: Optional[list[str]] = None) -> list[SwingSignal]:
    if dates is None:
        dates = list_available_dates()
    out: list[SwingSignal] = []
    for d in dates:
        out.extend(load_swing(d))
    out.sort(key=lambda s: (s.detected_epoch, s.source, s.ticker))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    dates = list_available_dates()
    print(f"available dates: {dates}")
    sigs = load_all()
    print(f"swing signals loaded: {len(sigs)}")

    by_source = {}
    for s in sigs:
        by_source[s.source] = by_source.get(s.source, 0) + 1
    print(f"by source: {by_source}")

    by_kind = {}
    for s in sigs:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    print("kinds:")
    for k in sorted(by_kind, key=lambda x: -by_kind[x]):
        print(f"  {k:35s} {by_kind[k]}")
