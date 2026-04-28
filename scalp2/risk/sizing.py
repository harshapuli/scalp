"""
risk/sizing.py — fractional Kelly with phase ramp + per-trade cap.

Per spec §3.1 (S5-E7/E8) + config/thresholds.yaml:
  per_trade_pct_paper:    0.005   (0.5% per trade in paper / ramp phases)
  per_trade_pct_full:     0.010   (1.0% per trade after Phase 7)

Phase ramp multipliers:
  sizing_p1: 0.25          (Phase 6 — first 30 trades)
  sizing_p2: 0.50          (Phase 7 — after 30 successful)
  sizing_full: 1.00        (after 100 cumulative live)

Math:
  b = ev_expected_at_win / |ev_expected_at_loss|
  p = posterior probability of win (Beta-Binomial point estimate)
  q = 1 - p
  kelly_full = (b·p - q) / b

  scale_oos   = clip(realized_60d_wr / predicted_60d_wr, 0, 1)
  scale_phase = ramp.sizing_<p1|p2|full>

  pos_size_pct = kelly_full · scale_oos · scale_phase
  HARD CAP    = per_trade_pct_<paper|full>

  if pos_size_pct > HARD CAP → pos_size_pct = HARD CAP

NOTE: replaces the inferred-scaffold module formerly at scalp2/src/kelly_progression.py.
The earlier module had a 5% hard cap and a 4-state PhaseState; both are wrong per
spec. Spec uses 0.5%/1.0% caps and the S5 phase-0..7 state machine (separate from
sizing — sizing only consumes TradingMode).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# Per-trade hard caps (spec §10.4 s5.risk)
PER_TRADE_PCT_PAPER = 0.005   # 0.5% — paper / ramp phases
PER_TRADE_PCT_FULL = 0.010    # 1.0% — after Phase 7 promotion

# Phase ramp multipliers (spec §10.4 s5.ramp)
RAMP_P1 = 0.25
RAMP_P2 = 0.50
RAMP_FULL = 1.00


class TradingMode(str, Enum):
    """Live trading mode — controls which per-trade cap applies.

    Maps to TRADING_MODE env var per spec §10.7.
    """
    PAPER = "paper"           # Phase 5 paper trading
    RAMP_25 = "ramp_25"       # Phase 6 — 0.25× Kelly, 30 trades
    RAMP_50 = "ramp_50"       # Phase 7 — 0.50× Kelly
    FULL = "full"             # Phase 7 — full Kelly after 100 cumulative


# Map mode → (cap, ramp scale)
_MODE_CONFIG = {
    TradingMode.PAPER:   (PER_TRADE_PCT_PAPER, 0.0),     # paper: log only, no real size
    TradingMode.RAMP_25: (PER_TRADE_PCT_PAPER, RAMP_P1),
    TradingMode.RAMP_50: (PER_TRADE_PCT_PAPER, RAMP_P2),
    TradingMode.FULL:    (PER_TRADE_PCT_FULL,  RAMP_FULL),
}


@dataclass
class SizingDecision:
    kelly_full: float           # raw Kelly math result (can be negative)
    scale_oos: float            # rolling out-of-sample win-rate alignment, [0, 1]
    scale_phase: float          # phase-ramp multiplier
    pos_size_pct: float         # final position size as fraction of equity, [0, hard_cap]
    hard_cap: float             # hard cap that applied (per-trade pct for mode)
    capped_by: Optional[str] = None    # 'hard_cap' | 'negative_full' | 'paper_mode' | None


def kelly_full_fraction(p: float, b: float) -> float:
    """Raw Kelly. p = win prob, b = odds (ev_win / |ev_loss|)."""
    if b <= 0:
        return 0.0
    return (b * p - (1 - p)) / b


def compute_size(p: float, b: float, scale_oos: float, mode: TradingMode) -> SizingDecision:
    """Compute final position size as a fraction of equity.

    Args:
        p: Posterior probability of win, [0, 1]
        b: Payoff odds (ev_at_win / |ev_at_loss|), > 0
        scale_oos: Rolling 60-day realized_wr / predicted_wr, clipped to [0, 1]
        mode: TradingMode (paper / ramp_25 / ramp_50 / full)

    Returns:
        SizingDecision with full math + final pos_size_pct.
    """
    full = kelly_full_fraction(p, b)
    cap, scale_phase = _MODE_CONFIG[mode]

    if full <= 0:
        return SizingDecision(
            kelly_full=full, scale_oos=scale_oos, scale_phase=scale_phase,
            pos_size_pct=0.0, hard_cap=cap, capped_by="negative_full",
        )

    if mode == TradingMode.PAPER:
        # Paper: compute math but don't size live
        return SizingDecision(
            kelly_full=full, scale_oos=scale_oos, scale_phase=scale_phase,
            pos_size_pct=0.0, hard_cap=cap, capped_by="paper_mode",
        )

    raw = full * max(0.0, min(1.0, scale_oos)) * scale_phase
    capped_by = None
    if raw > cap:
        raw = cap
        capped_by = "hard_cap"
    return SizingDecision(
        kelly_full=full, scale_oos=scale_oos, scale_phase=scale_phase,
        pos_size_pct=raw, hard_cap=cap, capped_by=capped_by,
    )


def compute_scale_oos(rolling_60d_realized_wr: float, rolling_60d_predicted_wr: float) -> float:
    """Out-of-sample alignment scale. Returns [0, 1].

    If realized_wr matches predicted_wr → 1.0 (full size).
    If realized_wr is half of predicted → 0.5 (half size).
    If realized_wr exceeds predicted → clipped at 1.0 (don't grow on luck).
    """
    if rolling_60d_predicted_wr <= 0:
        return 0.0
    raw = rolling_60d_realized_wr / rolling_60d_predicted_wr
    return max(0.0, min(1.0, raw))


if __name__ == "__main__":
    # Sanity: full Kelly with strong edge in FULL mode hits 1% cap
    d = compute_size(p=0.95, b=5.0, scale_oos=1.0, mode=TradingMode.FULL)
    print(f"[sizing] FULL p=0.95 b=5.0 oos=1.0 → pos_size={d.pos_size_pct:.4f} cap={d.hard_cap} capped_by={d.capped_by}")
    assert d.pos_size_pct <= PER_TRADE_PCT_FULL, "FULL mode breached 1% cap"

    # Sanity: paper mode → 0 sizing
    d2 = compute_size(p=0.95, b=5.0, scale_oos=1.0, mode=TradingMode.PAPER)
    print(f"[sizing] PAPER → pos_size={d2.pos_size_pct} capped_by={d2.capped_by}")
    assert d2.pos_size_pct == 0.0

    # Sanity: ramp_25 with no edge → 0
    d3 = compute_size(p=0.50, b=1.0, scale_oos=1.0, mode=TradingMode.RAMP_25)
    print(f"[sizing] RAMP_25 p=0.50 b=1.0 → pos_size={d3.pos_size_pct} (no edge → 0)")
    assert d3.pos_size_pct == 0.0

    # Sanity: ramp_25 with modest edge → kelly_full=0.40, ×1.0 oos × 0.25 phase = 0.10
    # but capped at 0.005 (0.5%) → hard_cap fires
    d4 = compute_size(p=0.60, b=2.0, scale_oos=1.0, mode=TradingMode.RAMP_25)
    print(f"[sizing] RAMP_25 p=0.60 b=2.0 → pos_size={d4.pos_size_pct:.4f} capped_by={d4.capped_by}")
    assert d4.pos_size_pct == PER_TRADE_PCT_PAPER

    print("[sizing] OK — caps enforced per spec §10.4 s5.risk + s5.ramp")
