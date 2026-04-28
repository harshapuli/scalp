"""risk/position_manager.py — IV-crush watcher, stops, timeouts. Spec S5-80.

vega-weighted IV-crush exit:
  position_vega × delta_iv > 0.5 × current_premium → exit
  Placeholder 35% IV until 30-trade calibration (S5-80 acceptance).

Tests required (S5-80.T2):
  After 30 paper trades, threshold derived from observed (vega × Δiv) / premium ratio.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class OptionPosition:
    """Open option position snapshot for exit-decision logic."""
    symbol: str
    strategy: str                # 'S5'
    ticker: str
    entry_premium: float
    current_premium: float
    vega: float                  # per-contract vega
    iv_at_entry: float
    iv_now: float
    opened_at: datetime
    direction: str               # 'long' | 'short'


@dataclass
class ExitDecision:
    should_exit: bool
    reason: Optional[str] = None
    pnl_pct: Optional[float] = None


def vega_weighted_iv_crush_exit(p: OptionPosition,
                                  vega_pct_of_premium_threshold: float = 0.50) -> ExitDecision:
    """S5-80 vega-weighted IV-crush. Returns exit decision.

    Rule: position_vega × delta_iv > vega_pct_of_premium_threshold × current_premium
          → exit (premium loss attributable to IV crush exceeds threshold).

    delta_iv is in % units (e.g. 0.20 = 20pp IV drop). vega is per 1pt IV.
    iv_loss_dollars = vega × delta_iv × 100 (since vega is per 1.0 of IV, not 0.01).
    """
    if p.iv_at_entry <= 0:
        return ExitDecision(should_exit=False, reason="no_entry_iv")

    delta_iv = p.iv_at_entry - p.iv_now
    if delta_iv <= 0:
        return ExitDecision(should_exit=False, reason="iv_not_crushed")

    iv_loss_dollars = p.vega * delta_iv * 100
    if p.current_premium <= 0:
        return ExitDecision(should_exit=True, reason="premium_zeroed")

    if iv_loss_dollars > vega_pct_of_premium_threshold * p.current_premium:
        return ExitDecision(
            should_exit=True,
            reason="iv_crush_vega_threshold",
            pnl_pct=(p.current_premium - p.entry_premium) / p.entry_premium,
        )
    return ExitDecision(should_exit=False)


def time_stop_exit(p: OptionPosition,
                    now: Optional[datetime] = None,
                    max_hold_hours: float = 24.0) -> ExitDecision:
    now = now or datetime.now(tz=timezone.utc)
    held = (now - p.opened_at).total_seconds() / 3600.0
    if held > max_hold_hours:
        return ExitDecision(should_exit=True, reason="time_stop",
                             pnl_pct=(p.current_premium - p.entry_premium) / p.entry_premium
                             if p.entry_premium > 0 else None)
    return ExitDecision(should_exit=False)


def hard_stop_exit(p: OptionPosition,
                    stop_pct: float = -0.50) -> ExitDecision:
    if p.entry_premium <= 0:
        return ExitDecision(should_exit=False)
    pnl = (p.current_premium - p.entry_premium) / p.entry_premium
    if pnl <= stop_pct:
        return ExitDecision(should_exit=True, reason="hard_stop", pnl_pct=pnl)
    return ExitDecision(should_exit=False)


def manage_position(p: OptionPosition,
                     iv_crush_threshold: float = 0.50,
                     hard_stop_pct: float = -0.50,
                     max_hold_hours: float = 24.0) -> ExitDecision:
    """Composite exit. First triggered exit reason wins."""
    for check in (
        lambda: hard_stop_exit(p, hard_stop_pct),
        lambda: vega_weighted_iv_crush_exit(p, iv_crush_threshold),
        lambda: time_stop_exit(p, max_hold_hours=max_hold_hours),
    ):
        d = check()
        if d.should_exit:
            return d
    return ExitDecision(should_exit=False)


def calibrate_iv_crush_threshold(closed_trades_iv_loss_ratios: list[float],
                                   percentile: float = 0.50) -> float:
    """S5-80.T2 / S5-100. Derive vega-weighted threshold from observed
    (iv_loss_dollars / current_premium) ratios at break-even."""
    if not closed_trades_iv_loss_ratios:
        return 0.50
    sorted_ratios = sorted(closed_trades_iv_loss_ratios)
    idx = max(0, int(len(sorted_ratios) * percentile) - 1)
    return float(sorted_ratios[idx])


if __name__ == "__main__":
    p = OptionPosition(
        symbol="SPY260508C00475000", strategy="S5", ticker="SPY",
        entry_premium=1.00, current_premium=0.60,
        vega=0.05, iv_at_entry=0.40, iv_now=0.20,
        opened_at=datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc),
        direction="long",
    )
    d = vega_weighted_iv_crush_exit(p)
    print(f"[pm] iv_crush: should_exit={d.should_exit} reason={d.reason}")
    assert d.should_exit, "IV crush should fire"

    p2 = OptionPosition(
        symbol="X", strategy="S5", ticker="X",
        entry_premium=1.00, current_premium=0.40,
        vega=0.05, iv_at_entry=0.40, iv_now=0.40,
        opened_at=datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc),
        direction="long",
    )
    d2 = hard_stop_exit(p2)
    print(f"[pm] hard_stop: should_exit={d2.should_exit} reason={d2.reason}")
    assert d2.should_exit
    print("[pm] OK")
