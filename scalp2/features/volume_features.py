"""features/volume_features.py — Volume features. SCALP-1.2.

  current_push_vol         — sum of volume on bars in the current directional push
  prior_push_vol           — sum of volume on the prior push (opposite direction)
  volume_divergence_ratio  — current_push_vol / prior_push_vol; <1 → divergence
  climax_vol_ratio         — current_bar_vol / mean(last_N_bars_vol); >2 → climax

A "push" is a contiguous run of bars where each close > prior close (long push)
or each close < prior close (short push). Bars with equal close break the run.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.price_structure import BarOHLC


def _last_two_pushes(bars: list[BarOHLC]) -> tuple[list[BarOHLC], list[BarOHLC]]:
    """Walk backwards from the last bar; return (current_push, prior_push)
    where each push is a sequence of bars in monotonic direction.

    Direction defined by close-to-close. Equal-close breaks a push.
    """
    if len(bars) < 2:
        return bars, []

    def _direction(later: BarOHLC, earlier: BarOHLC) -> int:
        if later.c > earlier.c:
            return 1
        if later.c < earlier.c:
            return -1
        return 0

    n = len(bars)
    # current push: starts from the last bar, walk back as long as direction matches
    current_dir = _direction(bars[-1], bars[-2])
    if current_dir == 0:
        return [bars[-1]], []
    cur_lo = n - 1
    while cur_lo > 0 and _direction(bars[cur_lo], bars[cur_lo - 1]) == current_dir:
        cur_lo -= 1
    current_push = bars[cur_lo:n]

    # prior push: opposite direction immediately before current push
    if cur_lo == 0:
        return current_push, []
    prior_dir = _direction(bars[cur_lo], bars[cur_lo - 1])
    # the bar at cur_lo-1 is the last bar of the prior push; walk back further
    prior_hi = cur_lo
    pri_lo = cur_lo - 1
    while pri_lo > 0 and _direction(bars[pri_lo], bars[pri_lo - 1]) == prior_dir:
        pri_lo -= 1
    prior_push = bars[pri_lo:prior_hi]
    return current_push, prior_push


def push_volumes(bars: list[BarOHLC]) -> tuple[float, float]:
    """Returns (current_push_vol, prior_push_vol)."""
    cur, pri = _last_two_pushes(bars)
    return sum(b.v for b in cur), sum(b.v for b in pri)


def volume_divergence_ratio(current_push_vol: float, prior_push_vol: float) -> float:
    """Ratio of current push volume to prior push.

    < 1.0 → current push has LESS volume than the prior (divergence — possible
            climax / reversal setup).
    1.0 ± noise → continuation of trend.
    > 1.5 → current push has substantially more volume (acceleration).
    """
    if prior_push_vol <= 0:
        return 1.0
    return current_push_vol / prior_push_vol


def climax_vol_ratio(bars: list[BarOHLC], lookback: int = 30) -> float:
    """current_bar_vol / mean(last_N_bars_vol)."""
    if not bars:
        return 1.0
    win = bars[-lookback:] if len(bars) >= lookback else bars
    mean_v = sum(b.v for b in win) / max(1, len(win))
    if mean_v <= 0:
        return 1.0
    return bars[-1].v / mean_v


if __name__ == "__main__":
    bars = []
    # Pattern: 3 down bars, 4 up bars; the up push starts at index 2 (98→99).
    # current push includes bars[2:7] inclusive (the bar just before the inflection
    # is the start of the up direction).
    for i, c in enumerate([100, 99, 98, 99, 100, 101, 102]):
        bars.append(BarOHLC(o=c - 0.5, h=c + 0.5, l=c - 0.7, c=c, v=1000 * (i + 1)))
    cur_v, pri_v = push_volumes(bars)
    print(f"[volume] current push vol={cur_v}")
    print(f"[volume] prior push vol={pri_v}")
    print(f"[volume] divergence_ratio={volume_divergence_ratio(cur_v, pri_v):.3f}")
    print(f"[volume] climax_vol_ratio={climax_vol_ratio(bars):.3f}")
    assert cur_v > 0 and pri_v > 0, "both pushes should have volume"
    print("[volume] OK")
