"""
loader_historical.py — ingest engine_scalp/scalp_all_signals.json.

The legacy engine_scalp archive is a single rolled-up JSON document with
4984 signals from 7 strategies covering 2019-04 through 2026-04. Schema:

  generated_utc: str
  n_signals: int
  n_strategies: int
  signals: [
    {
      strategy_id, strategy_label, category, evidence, sample_only,
      outcome, date, ticker, direction, trigger,
      entry_t, exit_t, exit_reason, minutes_held,
      entry_px, exit_px, pnl_dollars, pnl_pct
      # plus pnl_pct_of_max_loss for credit-spread strat,
      # plus extras{} for credit spreads / specialty strats
    }, ...
  ]

We expose a HistoricalSignal dataclass that's friendly to the existing
renderer and stats helpers — entry_epoch / exit_epoch / win flag / ticker /
strategy / trigger / direction / pnl_pct / minutes_held.

READ-ONLY on engine_scalp/. Path is hard-coded to the canonical location
since the archive moves rarely and the user keeps it under
"scalp QQQ/claude/gemini /engine_scalp/scalp_all_signals.json".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ENGINE_SCALP_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_scalp")
ARCHIVE_PATH = ENGINE_SCALP_ROOT / "scalp_all_signals.json"


# ──────────────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class HistoricalSignal:
    # identity
    strategy_id: str
    strategy_label: str
    category: str             # PRIMARY | LOSER | OPTIONAL | MARGINAL
    evidence: str             # REAL OPT | etc
    ticker: Optional[str]
    direction: Optional[str]  # CALL | PUT | None
    trigger: Optional[str]    # WASHOUT_BOUNCE / MOMENTUM_UP / etc

    # timing
    date_str: str             # "YYYY-MM-DD"
    entry_t: Optional[str]
    exit_t: Optional[str]
    entry_epoch: Optional[float]
    exit_epoch: Optional[float]
    minutes_held: Optional[int]

    # outcome
    outcome: str              # WIN | LOSS | FLAT | NO_FWD_OPTION_BARS
    exit_reason: Optional[str]
    entry_px: Optional[float]
    exit_px: Optional[float]
    pnl_dollars: Optional[float]
    pnl_pct: Optional[float]            # primary pnl_pct if present
    pnl_pct_of_max_loss: Optional[float]  # credit-spread variant

    # optional metadata
    sample_only: bool = False
    extras: dict = field(default_factory=dict, repr=False)
    raw: dict = field(default_factory=dict, repr=False)

    # Convenience flags
    @property
    def is_win(self) -> bool:
        return self.outcome == "WIN"

    @property
    def is_loss(self) -> bool:
        return self.outcome == "LOSS"

    @property
    def is_flat(self) -> bool:
        return self.outcome == "FLAT"

    @property
    def is_resolved(self) -> bool:
        return self.outcome in ("WIN", "LOSS", "FLAT")

    @property
    def month_str(self) -> str:
        return self.date_str[:7] if self.date_str else "—"

    @property
    def year_str(self) -> str:
        return self.date_str[:4] if self.date_str else "—"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _to_epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    s = str(iso).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def load_historical(path: Optional[Path] = None) -> list[HistoricalSignal]:
    """Read the archive JSON and return HistoricalSignal records."""
    fp = Path(path) if path else ARCHIVE_PATH
    if not fp.exists():
        return []

    try:
        data = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    raw_sigs = data.get("signals") or []
    out: list[HistoricalSignal] = []
    for d in raw_sigs:
        try:
            sig = HistoricalSignal(
                strategy_id=str(d.get("strategy_id") or "?"),
                strategy_label=str(d.get("strategy_label") or "?"),
                category=str(d.get("category") or "?"),
                evidence=str(d.get("evidence") or ""),
                ticker=d.get("ticker"),
                direction=d.get("direction"),
                trigger=d.get("trigger"),
                date_str=str(d.get("date") or ""),
                entry_t=d.get("entry_t"),
                exit_t=d.get("exit_t"),
                entry_epoch=_to_epoch(d.get("entry_t")),
                exit_epoch=_to_epoch(d.get("exit_t")),
                minutes_held=d.get("minutes_held"),
                outcome=str(d.get("outcome") or "?"),
                exit_reason=d.get("exit_reason"),
                entry_px=d.get("entry_px"),
                exit_px=d.get("exit_px"),
                pnl_dollars=d.get("pnl_dollars"),
                pnl_pct=d.get("pnl_pct"),
                pnl_pct_of_max_loss=d.get("pnl_pct_of_max_loss"),
                sample_only=bool(d.get("sample_only") or False),
                extras=d.get("extras") or {},
                raw=d,
            )
            out.append(sig)
        except Exception:
            # Skip malformed records — never blow up the loader
            continue

    # Sort by entry epoch (None entries to the end so they don't disrupt ordering)
    out.sort(key=lambda s: (s.entry_epoch is None, s.entry_epoch or 0.0))
    return out


def archive_meta(path: Optional[Path] = None) -> dict:
    """Return the archive's top-level metadata (generated_utc, n_signals, n_strategies)."""
    fp = Path(path) if path else ARCHIVE_PATH
    if not fp.exists():
        return {"available": False}
    try:
        data = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return {"available": False}
    return {
        "available": True,
        "path": str(fp),
        "generated_utc": data.get("generated_utc"),
        "n_signals": data.get("n_signals"),
        "n_strategies": data.get("n_strategies"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import time
    from collections import Counter

    t0 = time.time()
    meta = archive_meta()
    sigs = load_historical()
    dt = time.time() - t0

    print(f"loaded {len(sigs)} signals in {dt:.2f}s from {meta.get('path')}")
    print(f"archive generated: {meta.get('generated_utc')}")
    print(f"archive declares: {meta.get('n_signals')} signals, {meta.get('n_strategies')} strategies")
    print()

    if not sigs:
        raise SystemExit(0)

    # Categories
    cats = Counter(s.category for s in sigs)
    print(f"categories: {dict(cats)}")
    outc = Counter(s.outcome for s in sigs)
    print(f"outcomes:   {dict(outc)}")
    dirs = Counter(s.direction or "—" for s in sigs)
    print(f"directions: {dict(dirs)}")
    print()

    # Date range
    dates = sorted(set(s.date_str for s in sigs if s.date_str))
    print(f"date range: {dates[0]} → {dates[-1]} ({len(dates)} unique dates)")
    print()

    # Per-strategy
    strats = sorted(set(s.strategy_id for s in sigs))
    print(f"per-strategy ({len(strats)}):")
    print(f"  {'strategy':45s} {'n':>5s} {'wins':>5s} {'losses':>6s} {'win%':>6s} {'avg_pnl%':>9s}")
    for st in strats:
        ss = [s for s in sigs if s.strategy_id == st]
        n = len(ss)
        w = sum(1 for s in ss if s.is_win)
        loss = sum(1 for s in ss if s.is_loss)
        win_pct = 100 * w / max(n, 1)
        avg = sum((s.pnl_pct or 0.0) for s in ss) / max(n, 1)
        print(f"  {st:45s} {n:>5d} {w:>5d} {loss:>6d} {win_pct:>5.1f}% {avg:>+9.2f}")

    # Top tickers
    print()
    ticks = Counter(s.ticker or "—" for s in sigs)
    print(f"top 10 tickers:  {ticks.most_common(10)}")
