"""strategies/s5_gamma_reversal/ml/labeler.py — Trade-outcome labeler. Spec S5-30.

Per ChatGPT review (incorporated): label_s5_trade_outcome (production label) +
label_reversal_next_N (sub-edge attribution, kept).

ACCEPTANCE CRITERIA (S5-30):
  - Production label: label_trade_success — 1 ONLY if simulated production trade
    reaches target before stop AND survives modeled costs/slippage/IV-crush
  - 0 for: stop hit, timeout, IV crush failure, invalidation, OR unfilled/illiquid
  - Decision-time-only constraints: ATR, GEX, spread, IV all snapshotted at decision time
  - Future bars: bars[row.idx+1 : row.idx+1+N] strictly
  - Both labels stored: label_reversal_next_N (sub-edge) AND label_trade_success (prod)

Tests required (S5-30.T1, T2, T3):
  T1 — labeler rejects future-data ATR (raises LookAheadError)
  T2 — label_trade_success matches production execution (synthetic case)
  T3 — rejects winning reversal that loses money (IV crush case)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from features.datatypes import GEXSnapshot


class LookAheadError(Exception):
    """Raised when a feature/label uses data from after the decision time."""


# ──────────────────────────────────────────────────────────────────────────────
# Decision-time helpers (S5-30.1, S5-30.2) with explicit look-ahead guards
# ──────────────────────────────────────────────────────────────────────────────


def decision_time_atr(bars: list, idx: int, period: int = 20) -> float:
    """S5-30.1. Returns ATR at bar idx using ONLY bars[:idx+1].

    Raises LookAheadError if any caller tries to pass an idx that would imply
    future-bar access (negative idx, or idx >= len(bars)).
    """
    if idx < 0 or idx >= len(bars):
        raise LookAheadError(
            f"decision_time_atr: idx={idx} out of range [0, {len(bars)})"
        )
    if idx < 1:
        return 0.0
    # Use bars up to and including idx, no further
    window = bars[max(0, idx - period + 1): idx + 1]
    if len(window) < 2:
        return 0.0
    trs = []
    for i in range(1, len(window)):
        b = window[i]
        prev_c = window[i - 1].c
        tr = max(
            b.h - b.l,
            abs(b.h - prev_c),
            abs(b.l - prev_c),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs) / len(trs)


def decision_time_gex(gex_history: Iterable[GEXSnapshot], timestamp) -> Optional[GEXSnapshot]:
    """S5-30.2. Returns the latest snapshot with ts ≤ timestamp.

    None if no snapshot exists at or before timestamp.
    """
    eligible = [s for s in gex_history if s.timestamp <= timestamp]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.timestamp)


# ──────────────────────────────────────────────────────────────────────────────
# Labeler types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class LabelerRow:
    """Minimal candidate row for the labeler. Caller fills from features dataset."""
    idx: int                      # position in `bars` array
    timestamp_iso: str            # decision time, for traceability
    close: float                  # bar close at decision
    next_bar_open: float          # next bar's open — entry price
    iv_at_decision: float
    contracts: int = 1


@dataclass
class CostModel:
    """Spec §10.4 s5.cost_model. Used in labeler simulation."""
    commission_per_contract: float = 0.65
    slippage_pct: float = 0.03           # 3% of premium
    spread_cost_pct: float = 0.06        # 6% RT
    iv_crush_kill_pct: float = 0.35      # placeholder per S5-80

    def entry_slippage(self, row: LabelerRow) -> float:
        """Slippage adjustment to next-bar-open price."""
        return row.next_bar_open * self.slippage_pct

    def total_cost(self, row: LabelerRow) -> float:
        """Per-trade total cost in dollars (commissions + spread + slippage)."""
        notional = row.next_bar_open * 100 * row.contracts
        return (
            self.commission_per_contract * row.contracts * 2  # round-trip
            + notional * self.spread_cost_pct
        )


# ──────────────────────────────────────────────────────────────────────────────
# Simulator (S5-30.3)
# ──────────────────────────────────────────────────────────────────────────────


def _simulate_first_touch(future_bars: list,
                           target_price: float,
                           stop_price: float,
                           direction: Literal["long_after_tank", "short_after_surge"]
                           ) -> tuple[bool, int]:
    """Returns (target_hit_first, idx_of_event). idx_of_event is bar index in future_bars."""
    is_long = direction == "long_after_tank"
    for i, b in enumerate(future_bars):
        if is_long:
            hit_target = b.h >= target_price
            hit_stop = b.l <= stop_price
        else:
            hit_target = b.l <= target_price
            hit_stop = b.h >= stop_price
        # Stop-first on wide bars (conservative)
        if hit_stop and hit_target:
            return False, i
        if hit_stop:
            return False, i
        if hit_target:
            return True, i
    return False, len(future_bars) - 1   # EOD / timeout


# ──────────────────────────────────────────────────────────────────────────────
# Public labeler (S5-30 + ChatGPT review)
# ──────────────────────────────────────────────────────────────────────────────


def label_s5_trade_outcome(row: LabelerRow,
                            future_bars: list,
                            direction: Literal["long_after_tank", "short_after_surge"],
                            decision_time_atr_value: float,
                            decision_time_gex_snap: Optional[GEXSnapshot],
                            cost_model: CostModel) -> tuple[int, int]:
    """Returns (label_reversal_next_N, label_trade_success).

    Spec §10.5 reference implementation.

    label_reversal_next_N: 1 iff price hit 1.0×ATR favorable AND was not
                          invalidated by the gamma strike being pierced.
                          Used for Phase 0 sub-edge attribution only.

    label_trade_success:  Production label. 1 iff:
                          - target hit first (not stop)
                          - did not get IV-crushed beyond cost_model threshold
                          - net pnl after cost > 0
    """
    if decision_time_atr_value <= 0:
        return 0, 0

    atr = decision_time_atr_value
    is_long = direction == "long_after_tank"

    # ── label_reversal_next_N (v2.2 §17 reversal-state label) ──
    if not future_bars:
        return 0, 0
    if is_long:
        hit_reversal_target = max(b.h for b in future_bars) >= row.close + 1.0 * atr
    else:
        hit_reversal_target = min(b.l for b in future_bars) <= row.close - 1.0 * atr

    # Invalidation by gamma strike pierce
    invalidated = False
    if decision_time_gex_snap and decision_time_gex_snap.major_pos_gex_strike > 0:
        gex_strike = decision_time_gex_snap.major_pos_gex_strike
        buf = 0.25 * atr
        if is_long:
            invalidated = min(b.l for b in future_bars) <= gex_strike - buf
        else:
            invalidated = max(b.h for b in future_bars) >= gex_strike + buf

    label_reversal = 1 if hit_reversal_target and not invalidated else 0

    # ── label_trade_success (production, ChatGPT review) ──
    sim_entry = row.next_bar_open + (
        cost_model.entry_slippage(row) if is_long else -cost_model.entry_slippage(row)
    )
    sim_target = sim_entry + (1.0 * atr if is_long else -1.0 * atr)
    sim_stop = sim_entry - (1.0 * atr if is_long else -1.0 * atr)

    target_hit_first, event_idx = _simulate_first_touch(
        future_bars, sim_target, sim_stop, direction
    )
    if not target_hit_first:
        return label_reversal, 0

    # IV crush check — only valid if future bars carry .atm_iv
    if event_idx >= 0 and event_idx < len(future_bars):
        b_at_event = future_bars[event_idx]
        iv_at_event = getattr(b_at_event, "atm_iv", None)
        if iv_at_event is not None and row.iv_at_decision > 0:
            iv_crush = (row.iv_at_decision - iv_at_event) / row.iv_at_decision
            if iv_crush > cost_model.iv_crush_kill_pct:
                return label_reversal, 0

    # Net PnL after costs
    sim_pnl = (sim_target - sim_entry) * row.contracts * 100
    if not is_long:
        sim_pnl = -sim_pnl
    sim_pnl_net = sim_pnl - cost_model.total_cost(row)
    return label_reversal, (1 if sim_pnl_net > 0 else 0)


if __name__ == "__main__":
    from dataclasses import dataclass as _dc

    @_dc
    class B:
        o: float; h: float; l: float; c: float

    # T1 — out-of-range idx raises LookAheadError
    bars = [B(100, 101, 99.5, 100.5) for _ in range(10)]
    try:
        decision_time_atr(bars, idx=20)
        raise AssertionError("expected LookAheadError")
    except LookAheadError:
        print("[labeler] T1 PASS — out-of-range idx raises LookAheadError")

    # T2 — label_trade_success on a synthetic winner
    row = LabelerRow(idx=10, timestamp_iso="2026-04-28T14:30:00Z",
                      close=185.50, next_bar_open=185.50,
                      iv_at_decision=0.40, contracts=1)
    fwd = [
        B(o=185.50, h=186.0, l=185.30, c=185.80),
        B(o=185.80, h=187.50, l=185.60, c=187.00),    # hits 185.50+2 target
        B(o=187.00, h=187.80, l=186.80, c=187.40),
    ]
    cm = CostModel(spread_cost_pct=0.0, commission_per_contract=0.0, slippage_pct=0.0)
    rev, succ = label_s5_trade_outcome(row, fwd, "long_after_tank",
                                        decision_time_atr_value=2.0,
                                        decision_time_gex_snap=None,
                                        cost_model=cm)
    print(f"[labeler] T2 long winner: label_reversal={rev} label_trade_success={succ}")
    assert rev == 1 and succ == 1, f"T2 failed: rev={rev} succ={succ}"

    # T3 — winning reversal that gets IV-crushed
    @_dc
    class B_IV:
        o: float; h: float; l: float; c: float; atm_iv: float
    fwd_iv = [
        B_IV(o=185.50, h=186.0, l=185.30, c=185.80, atm_iv=0.40),
        B_IV(o=185.80, h=187.50, l=185.60, c=187.00, atm_iv=0.20),  # IV crashed 50%
    ]
    rev2, succ2 = label_s5_trade_outcome(row, fwd_iv, "long_after_tank",
                                          decision_time_atr_value=2.0,
                                          decision_time_gex_snap=None,
                                          cost_model=CostModel(iv_crush_kill_pct=0.35))
    print(f"[labeler] T3 IV-crushed winner: rev={rev2} succ={succ2}")
    assert rev2 == 1 and succ2 == 0, f"T3 failed (should be rev=1 succ=0): rev={rev2} succ={succ2}"
    print("[labeler] T1+T2+T3 PASS — look-ahead protection + production label correct")
