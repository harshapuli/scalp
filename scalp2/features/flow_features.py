"""features/flow_features.py — Options-flow features. SCALP-1.5 + S4-1.

  net_signed_premium_5m / 30m  — call_buy − put_buy − call_sell + put_sell over window
  call_ask_pct / put_ask_pct   — fraction of premium hitting ask (aggressive)
  sweep_count                  — number of sweep-tagged trades
  flow_flip                    — 5m flow opposite of 30m?
  signed_flow_score            — z-score combo in [-1, +1] (S4-1.T1, T2)

Inputs are FlowRecord lists from data_clients.unusual_whales.flow_recent().
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import FlowRecord


def _within_window(records: Iterable[FlowRecord],
                    cutoff: datetime,
                    window_minutes: int) -> list[FlowRecord]:
    lo = cutoff - timedelta(minutes=window_minutes)
    return [r for r in records if lo <= r.timestamp <= cutoff]


def net_signed_premium(records: Iterable[FlowRecord],
                        cutoff: datetime,
                        window_minutes: int = 5) -> float:
    """Returns net signed dollar flow over the window.

    Positive = bullish bias (call buys + put sells dominate).
    Negative = bearish bias.
    """
    win = _within_window(records, cutoff, window_minutes)
    net = 0.0
    for r in win:
        if r.side == "call" and r.action == "buy":
            net += r.premium
        elif r.side == "call" and r.action == "sell":
            net -= r.premium
        elif r.side == "put" and r.action == "buy":
            net -= r.premium
        elif r.side == "put" and r.action == "sell":
            net += r.premium
    return net


def call_ask_pct(records: Iterable[FlowRecord],
                  cutoff: datetime,
                  window_minutes: int = 5) -> float:
    """Fraction of CALL premium that hit the ask (aggressive bullish)."""
    win = _within_window(records, cutoff, window_minutes)
    call_total = 0.0
    call_at_ask = 0.0
    for r in win:
        if r.side != "call":
            continue
        call_total += r.premium
        if r.action == "buy":
            call_at_ask += r.premium
    if call_total <= 0:
        return 0.0
    return call_at_ask / call_total


def put_ask_pct(records: Iterable[FlowRecord],
                 cutoff: datetime,
                 window_minutes: int = 5) -> float:
    """Fraction of PUT premium that hit the ask (aggressive bearish)."""
    win = _within_window(records, cutoff, window_minutes)
    put_total = 0.0
    put_at_ask = 0.0
    for r in win:
        if r.side != "put":
            continue
        put_total += r.premium
        if r.action == "buy":
            put_at_ask += r.premium
    if put_total <= 0:
        return 0.0
    return put_at_ask / put_total


def sweep_count(records: Iterable[FlowRecord],
                 cutoff: datetime,
                 window_minutes: int = 5) -> int:
    return sum(1 for r in _within_window(records, cutoff, window_minutes) if r.is_sweep)


def flow_flip(records: Iterable[FlowRecord], cutoff: datetime) -> bool:
    """Did 5m flow direction flip vs 30m flow direction?"""
    five = net_signed_premium(records, cutoff, window_minutes=5)
    thirty = net_signed_premium(records, cutoff, window_minutes=30)
    return (five > 0) != (thirty > 0) and (five != 0) and (thirty != 0)


def signed_flow_score(records: Iterable[FlowRecord],
                       cutoff: datetime,
                       window_minutes: int = 30,
                       baseline_dollars: float = 5_000_000) -> float:
    """Returns score in [-1, +1] combining magnitude + side aggressiveness.

    Heuristic combination:
      net_z = clip(net_signed_premium / baseline, -1, 1)
      ask_alignment = call_ask - put_ask  (in [-1, +1])
      sweep_factor = min(1.0, sweep_count / 10)

    Final score = 0.6 · net_z + 0.3 · ask_alignment + 0.1 · sweep_factor·sign(net_z)

    Tests (S4-1.T1, T2):
      Pure call buys at ask → score >= 0.85
      Balanced flow → score in [-0.10, +0.10]
    """
    win = _within_window(records, cutoff, window_minutes)
    if not win:
        return 0.0

    net = net_signed_premium(records, cutoff, window_minutes)
    net_z = max(-1.0, min(1.0, net / baseline_dollars))

    call_ask = call_ask_pct(records, cutoff, window_minutes)
    put_ask = put_ask_pct(records, cutoff, window_minutes)
    # ask_alignment: high call_ask + low put_ask → +; opposite → -
    ask_alignment = call_ask - put_ask  # range [-1, 1]

    sweeps = sweep_count(records, cutoff, window_minutes)
    sweep_factor = min(1.0, sweeps / 10.0)

    sign_net = 1.0 if net_z >= 0 else -1.0
    raw = 0.6 * net_z + 0.3 * ask_alignment + 0.1 * sweep_factor * sign_net
    return max(-1.0, min(1.0, raw))


if __name__ == "__main__":
    from datetime import date as _date
    now = datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc)
    # T1 — pure call buys at ask
    pure_calls = [
        FlowRecord(
            ticker="NVDA", timestamp=now - timedelta(minutes=i),
            side="call", action="buy",
            premium=1_000_000, is_sweep=True,
            expiry=_date(2026, 5, 15), strike=200, iv_at_trade=0.4,
        )
        for i in range(5)
    ]
    s = signed_flow_score(pure_calls, now)
    print(f"[flow] T1 pure call buys score = {s:+.3f}  (expect ≥ 0.85)")
    assert s >= 0.85, f"T1 failed: {s}"

    # T2 — balanced
    balanced = pure_calls + [
        FlowRecord(
            ticker="NVDA", timestamp=now - timedelta(minutes=i),
            side="put", action="buy",
            premium=1_000_000, is_sweep=True,
            expiry=_date(2026, 5, 15), strike=180, iv_at_trade=0.4,
        )
        for i in range(5)
    ]
    s2 = signed_flow_score(balanced, now)
    print(f"[flow] T2 balanced score = {s2:+.3f}  (expect [-0.10, +0.10])")
    assert -0.10 <= s2 <= 0.10, f"T2 failed: {s2}"
    print("[flow] T1 + T2 PASS")
