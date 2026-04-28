"""
stats_historical.py — analytical cuts on the engine_scalp historical archive.

Unlike the live engine_v4 outcomes (which we resimulate via ATR ladders),
the engine_scalp archive already carries realized pnl_pct, exit_reason,
and outcome labels. So our cuts here aggregate those directly:

  - per strategy_id
  - per ticker
  - per trigger
  - per direction (CALL / PUT / —)
  - per category (PRIMARY / LOSER / OPTIONAL / MARGINAL)
  - per month (YYYY-MM)
  - per exit_reason

Each cut returns a list of HistoricalSliceStat sorted by realized EV (desc).
We re-use Wilson-95% CI from stats.py so the suppression rules stay aligned
with the live dashboard.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from loader_historical import HistoricalSignal
from stats import wilson_ci, percentile, MIN_N_DEFAULT


@dataclass
class HistoricalSliceStat:
    key: str                            # the dimension value (strategy / ticker / month / ...)
    n: int = 0
    n_wins: int = 0
    n_losses: int = 0
    n_flat: int = 0
    n_unresolved: int = 0
    win_pct: Optional[float] = None
    win_ci_lo: Optional[float] = None
    win_ci_hi: Optional[float] = None
    ev_mean_pct: Optional[float] = None
    ev_median_pct: Optional[float] = None
    ev_p25_pct: Optional[float] = None
    ev_p75_pct: Optional[float] = None
    avg_minutes: Optional[float] = None
    n_call: int = 0
    n_put: int = 0
    suppressed: bool = False
    extras: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Core summarizer
# ──────────────────────────────────────────────────────────────────────────────


def _summarize(key: str, sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> HistoricalSliceStat:
    n = len(sigs)
    stat = HistoricalSliceStat(key=key, n=n)

    if n == 0:
        stat.suppressed = True
        return stat

    stat.n_wins  = sum(1 for s in sigs if s.is_win)
    stat.n_losses = sum(1 for s in sigs if s.is_loss)
    stat.n_flat  = sum(1 for s in sigs if s.is_flat)
    stat.n_unresolved = n - (stat.n_wins + stat.n_losses + stat.n_flat)
    stat.n_call = sum(1 for s in sigs if s.direction == "CALL")
    stat.n_put = sum(1 for s in sigs if s.direction == "PUT")

    decided = stat.n_wins + stat.n_losses
    if decided > 0:
        stat.win_pct = 100.0 * stat.n_wins / decided
        lo, hi = wilson_ci(stat.n_wins, decided)
        stat.win_ci_lo = lo
        stat.win_ci_hi = hi

    pnls = [s.pnl_pct for s in sigs if s.pnl_pct is not None]
    if pnls:
        stat.ev_mean_pct = statistics.mean(pnls)
        stat.ev_median_pct = statistics.median(pnls)
        stat.ev_p25_pct = percentile(pnls, 25.0)
        stat.ev_p75_pct = percentile(pnls, 75.0)

    mins = [s.minutes_held for s in sigs if s.minutes_held is not None]
    if mins:
        stat.avg_minutes = sum(mins) / len(mins)

    if n < min_n:
        stat.suppressed = True

    return stat


def _slice_and_summarize(sigs: list[HistoricalSignal],
                         key_fn: Callable[[HistoricalSignal], str],
                         min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    """Group by key_fn, summarize each bucket, return sorted by EV desc."""
    buckets: dict[str, list[HistoricalSignal]] = defaultdict(list)
    for s in sigs:
        k = key_fn(s)
        if k is None:
            k = "—"
        buckets[str(k)].append(s)

    rows = [_summarize(k, ss, min_n=min_n) for k, ss in buckets.items()]
    # EV desc for performance dashboards; suppressed/None at the end
    rows.sort(key=lambda r: (r.suppressed, -(r.ev_mean_pct if r.ev_mean_pct is not None else -1e9)))
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Public cuts
# ──────────────────────────────────────────────────────────────────────────────


def by_strategy(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.strategy_label or s.strategy_id, min_n=min_n)


def by_ticker(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.ticker or "—", min_n=min_n)


def by_trigger(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.trigger or "—", min_n=min_n)


def by_direction(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.direction or "—", min_n=min_n)


def by_category(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.category or "—", min_n=min_n)


def by_month(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    rows = _slice_and_summarize(sigs, lambda s: s.month_str, min_n=min_n)
    # months should be chronological, not EV-sorted
    rows.sort(key=lambda r: r.key)
    return rows


def by_year(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    rows = _slice_and_summarize(sigs, lambda s: s.year_str, min_n=min_n)
    rows.sort(key=lambda r: r.key)
    return rows


def by_exit_reason(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(sigs, lambda s: s.exit_reason or "—", min_n=min_n)


def by_strategy_and_ticker(sigs: list[HistoricalSignal], min_n: int = MIN_N_DEFAULT) -> list[HistoricalSliceStat]:
    return _slice_and_summarize(
        sigs,
        lambda s: f"{(s.strategy_label or s.strategy_id)} · {s.ticker or '—'}",
        min_n=min_n,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Top-line numbers
# ──────────────────────────────────────────────────────────────────────────────


def top_line(sigs: list[HistoricalSignal]) -> dict:
    """Returns the headline stats for the Historical tab top stat-cards."""
    n = len(sigs)
    if not n:
        return {"n": 0}

    n_win = sum(1 for s in sigs if s.is_win)
    n_loss = sum(1 for s in sigs if s.is_loss)
    n_flat = sum(1 for s in sigs if s.is_flat)
    decided = n_win + n_loss

    pnls = [s.pnl_pct for s in sigs if s.pnl_pct is not None]
    ev_mean = statistics.mean(pnls) if pnls else None
    ev_median = statistics.median(pnls) if pnls else None

    win_pct = (100.0 * n_win / decided) if decided else None
    win_ci = wilson_ci(n_win, decided) if decided else (None, None)

    dates = sorted(set(s.date_str for s in sigs if s.date_str))
    tickers = set(s.ticker for s in sigs if s.ticker)
    strategies = set(s.strategy_id for s in sigs)

    n_call = sum(1 for s in sigs if s.direction == "CALL")
    n_put = sum(1 for s in sigs if s.direction == "PUT")

    return {
        "n": n,
        "n_win": n_win,
        "n_loss": n_loss,
        "n_flat": n_flat,
        "n_decided": decided,
        "n_call": n_call,
        "n_put": n_put,
        "win_pct": win_pct,
        "win_ci_lo": win_ci[0],
        "win_ci_hi": win_ci[1],
        "ev_mean_pct": ev_mean,
        "ev_median_pct": ev_median,
        "n_dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "n_tickers": len(tickers),
        "n_strategies": len(strategies),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Top-N performers / worst-N
# ──────────────────────────────────────────────────────────────────────────────


def top_n_signals(sigs: list[HistoricalSignal], n: int = 25, by_pnl: bool = True) -> list[HistoricalSignal]:
    """Top-N by realized pnl_pct."""
    have = [s for s in sigs if s.pnl_pct is not None]
    have.sort(key=lambda s: s.pnl_pct, reverse=by_pnl)
    return have[:n]


def worst_n_signals(sigs: list[HistoricalSignal], n: int = 25) -> list[HistoricalSignal]:
    have = [s for s in sigs if s.pnl_pct is not None]
    have.sort(key=lambda s: s.pnl_pct)
    return have[:n]


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from loader_historical import load_historical

    sigs = load_historical()
    print(f"loaded {len(sigs)} historical signals")
    print()

    tl = top_line(sigs)
    print("top-line:")
    for k, v in tl.items():
        if isinstance(v, float):
            print(f"  {k:18s} {v:+.2f}")
        else:
            print(f"  {k:18s} {v}")
    print()

    print("by_strategy (top 10 by EV):")
    for r in by_strategy(sigs)[:10]:
        ev = f"{r.ev_mean_pct:+.2f}%" if r.ev_mean_pct is not None else "—"
        wp = f"{r.win_pct:.1f}%" if r.win_pct is not None else "—"
        print(f"  {r.key:45s} n={r.n:>5d}  win={wp:>7s}  ev={ev:>9s}")

    print()
    print("by_ticker (top 10 by EV, n>=10):")
    rows = [r for r in by_ticker(sigs) if r.n >= 10]
    for r in rows[:10]:
        ev = f"{r.ev_mean_pct:+.2f}%" if r.ev_mean_pct is not None else "—"
        wp = f"{r.win_pct:.1f}%" if r.win_pct is not None else "—"
        print(f"  {r.key:10s} n={r.n:>5d}  win={wp:>7s}  ev={ev:>9s}  CALL={r.n_call} PUT={r.n_put}")

    print()
    print("by_trigger (top 10 by EV):")
    for r in by_trigger(sigs)[:10]:
        ev = f"{r.ev_mean_pct:+.2f}%" if r.ev_mean_pct is not None else "—"
        wp = f"{r.win_pct:.1f}%" if r.win_pct is not None else "—"
        print(f"  {r.key:30s} n={r.n:>5d}  win={wp:>7s}  ev={ev:>9s}")

    print()
    print("top 10 winning signals:")
    for s in top_n_signals(sigs, 10):
        print(f"  {s.date_str} {s.ticker or '—':6s} {s.direction or '—':5s} "
              f"{s.strategy_id:35s} pnl={s.pnl_pct:+.1f}% ({s.exit_reason})")

    print()
    print("worst 10 signals:")
    for s in worst_n_signals(sigs, 10):
        print(f"  {s.date_str} {s.ticker or '—':6s} {s.direction or '—':5s} "
              f"{s.strategy_id:35s} pnl={s.pnl_pct:+.1f}% ({s.exit_reason})")
