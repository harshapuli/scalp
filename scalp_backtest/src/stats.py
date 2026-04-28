"""
stats.py — Wilson 95% CI, realized EV per fire, n<5 suppression, slice helpers.

Design contract: DESIGN.md §10, §14 Q11 (Wilson CI), §14 Q12 (n<5 suppression),
§14 Q10 (EV/fire is the headline number).

Public API:
    wilson_ci(wins, n, z=1.96) -> (lo, hi)
    summarize_outcomes(outcomes, label_field='label_option_5p5x',
                       ev_field='ev_option_5p5x_stop_first', min_n=5) -> SliceStat
    slice_by(outcomes, key_fn) -> dict[key, list[Outcome]]
    cell_suppress(stat, min_n=5) -> SliceStat (zeroes/Nones if n < min_n)
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


MIN_N_DEFAULT = 5


@dataclass
class SliceStat:
    n: int = 0
    n_wins: int = 0
    n_losses: int = 0
    n_zeros: int = 0
    win_pct: Optional[float] = None
    win_ci_lo: Optional[float] = None
    win_ci_hi: Optional[float] = None
    ev_mean_pct: Optional[float] = None         # realized EV per fire (%)
    ev_median_pct: Optional[float] = None
    ev_p25_pct: Optional[float] = None
    ev_p75_pct: Optional[float] = None
    ev_stdev_pct: Optional[float] = None
    suppressed: bool = False                    # True when n < min_n
    extras: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Wilson 95% CI
# ──────────────────────────────────────────────────────────────────────────────


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = (centre - half) / denom
    hi = (centre + half) / denom
    return max(0.0, lo) * 100.0, min(1.0, hi) * 100.0


# ──────────────────────────────────────────────────────────────────────────────
# Slice helpers
# ──────────────────────────────────────────────────────────────────────────────


def slice_by(outcomes: Iterable, key_fn: Callable) -> dict:
    """Group outcomes by key_fn(outcome) -> hashable."""
    out: dict = {}
    for o in outcomes:
        k = key_fn(o)
        out.setdefault(k, []).append(o)
    return out


def percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q / 100.0 * (len(s) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ──────────────────────────────────────────────────────────────────────────────
# Summarize a slice
# ──────────────────────────────────────────────────────────────────────────────


def summarize_outcomes(outcomes: list,
                       label_field: str = "label_option_5p5x",
                       ev_field: str = "ev_option_5p5x_stop_first",
                       min_n: int = MIN_N_DEFAULT) -> SliceStat:
    """
    Compute SliceStat for a list of Outcomes.

    label_field   — which label column to count wins/losses on
    ev_field      — which EV column to mean (realized EV per fire is the headline)
    """
    n = len(outcomes)
    stat = SliceStat(n=n)

    if n == 0:
        stat.suppressed = True
        return stat

    labels = [getattr(o, label_field, None) for o in outcomes]
    n_wins   = sum(1 for L in labels if L == 1)
    n_losses = sum(1 for L in labels if L == -1)
    n_zeros  = sum(1 for L in labels if L == 0)
    stat.n_wins = n_wins
    stat.n_losses = n_losses
    stat.n_zeros = n_zeros

    decided = n_wins + n_losses + n_zeros
    if decided > 0:
        stat.win_pct = n_wins / decided * 100.0
        lo, hi = wilson_ci(n_wins, decided)
        stat.win_ci_lo, stat.win_ci_hi = lo, hi

    evs = [getattr(o, ev_field, None) for o in outcomes]
    evs = [e for e in evs if e is not None]
    if evs:
        stat.ev_mean_pct = statistics.mean(evs)
        stat.ev_median_pct = statistics.median(evs)
        stat.ev_p25_pct = percentile(evs, 25.0)
        stat.ev_p75_pct = percentile(evs, 75.0)
        if len(evs) >= 2:
            stat.ev_stdev_pct = statistics.stdev(evs)

    if n < min_n:
        stat.suppressed = True

    return stat


def cell_suppress(stat: SliceStat, min_n: int = MIN_N_DEFAULT) -> SliceStat:
    """Mark slice as suppressed if n < min_n (UI hides numbers, shows '—')."""
    if stat.n < min_n:
        stat.suppressed = True
    return stat


# ──────────────────────────────────────────────────────────────────────────────
# Convenience: build a kind table (the headline view)
# ──────────────────────────────────────────────────────────────────────────────


def kind_table(outcomes: list,
               label_field: str = "label_option_5p5x",
               ev_field: str = "ev_option_5p5x_stop_first",
               min_n: int = MIN_N_DEFAULT) -> dict[str, SliceStat]:
    by_kind = slice_by(outcomes, key_fn=lambda o: o.kind)
    return {k: summarize_outcomes(rows, label_field, ev_field, min_n)
            for k, rows in by_kind.items()}


def conf_band_table(outcomes: list,
                    label_field: str = "label_option_5p5x",
                    ev_field: str = "ev_option_5p5x_stop_first",
                    min_n: int = MIN_N_DEFAULT) -> dict[tuple[str, str], SliceStat]:
    """3-band view: kind × conf_band → stat."""
    by = slice_by(outcomes, key_fn=lambda o: (o.kind, getattr(o, "conf_band", "unknown")))
    return {k: summarize_outcomes(rows, label_field, ev_field, min_n)
            for k, rows in by.items()}


def time_of_day_table(outcomes: list,
                      label_field: str = "label_option_5p5x",
                      ev_field: str = "ev_option_5p5x_stop_first",
                      min_n: int = MIN_N_DEFAULT) -> dict[tuple[str, str], SliceStat]:
    """kind × time_of_day_bucket → stat (for heatmap)."""
    by = slice_by(outcomes, key_fn=lambda o: (o.kind, getattr(o, "time_of_day_bucket", "unknown")))
    return {k: summarize_outcomes(rows, label_field, ev_field, min_n)
            for k, rows in by.items()}


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from loader_scalp import load_all
    from outcome_intraday import compute_all, dedup_signals

    sigs, bars = load_all()
    sigs_d = dedup_signals(sigs)
    outs = compute_all(sigs_d, bars)
    print(f"outcomes: {len(outs)} (deduped)\n")

    # Headline kind table — option 5.5× cost-aware
    kt = kind_table(outs)
    print(f"{'KIND':22s} {'n':>4s} {'win%':>6s} {'CI_lo':>6s} {'CI_hi':>6s} {'EV/fire':>9s} {'p25..p75':>16s}")
    print("─" * 80)
    for kind in sorted(kt, key=lambda k: -kt[k].n):
        s = kt[kind]
        if s.suppressed:
            print(f"{kind:22s} {s.n:>4d}   —     —      —       —              —")
            continue
        wp = f"{s.win_pct:5.1f}%" if s.win_pct is not None else "  —  "
        ci_l = f"{s.win_ci_lo:5.1f}" if s.win_ci_lo is not None else "  —  "
        ci_h = f"{s.win_ci_hi:5.1f}" if s.win_ci_hi is not None else "  —  "
        ev = f"{s.ev_mean_pct:+.2f}%" if s.ev_mean_pct is not None else "  —   "
        p25 = s.ev_p25_pct if s.ev_p25_pct is not None else 0.0
        p75 = s.ev_p75_pct if s.ev_p75_pct is not None else 0.0
        rng = f"{p25:+.1f}..{p75:+.1f}"
        print(f"{kind:22s} {s.n:>4d} {wp:>6s} {ci_l:>6s} {ci_h:>6s} {ev:>9s} {rng:>16s}")

    print("\n— conf band slice (kind × band, only n ≥ 5) —")
    cbt = conf_band_table(outs)
    for (kind, band), s in sorted(cbt.items(), key=lambda kv: -kv[1].n):
        if s.suppressed:
            continue
        ev = f"{s.ev_mean_pct:+.2f}%" if s.ev_mean_pct is not None else "—"
        wp = f"{s.win_pct:.1f}%" if s.win_pct is not None else "—"
        print(f"  {kind:22s} {band:8s} n={s.n:>3d} win={wp:>7s} EV={ev:>8s}")
