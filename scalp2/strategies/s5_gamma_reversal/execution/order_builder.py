"""strategies/s5_gamma_reversal/execution/order_builder.py — Alpaca mleg constructor.

Per spec §10.5 build_s5_order pseudocode. Validates debit_to_width_max,
option_spread_pct_max gates BEFORE submission.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from features.datatypes import Candidate, Decision, OptionLeg, VerticalOrder, TRADE, PASS
from strategies.s5_gamma_reversal.execution.option_selector import (
    ContractRow, choose_expiry, choose_long_leg, choose_short_leg,
)


def _to_option_leg(c: ContractRow) -> OptionLeg:
    return OptionLeg(
        symbol=c.symbol, strike=c.strike, delta=c.delta,
        bid=c.bid, ask=c.ask,
        open_interest=c.open_interest, volume=c.volume,
    )


def build_s5_order(candidate: Candidate,
                    chain: Iterable[ContractRow],
                    cfg: dict,
                    today: Optional[date] = None) -> Decision:
    """Spec §10.5. Returns Decision (TRADE with VerticalOrder, or PASS with reason).

    Gates inside (in order):
      1. choose_expiry returns valid expiry in [dte_min, dte_max]
      2. choose_long_leg returns valid leg in delta band
      3. choose_short_leg returns valid leg
      4. debit / spread_width <= debit_to_width_max
      5. spread_pct <= option_spread_pct_max
    """
    today = today or date.today()
    ex = cfg["s5"]["execution"]
    side = "put" if candidate.direction == "short" else "call"

    expiry = choose_expiry(chain, today, ex["dte_min"], ex["dte_max"])
    if expiry is None:
        return PASS("no_eligible_expiry", strategy="S5", candidate_id=candidate.id)

    long_leg = choose_long_leg(chain, expiry, side, ex["delta_min"], ex["delta_max"])
    if long_leg is None:
        return PASS("no_eligible_long_leg", strategy="S5", candidate_id=candidate.id)

    short_leg = choose_short_leg(chain, long_leg, target_width=candidate.spread_width or 5.0)
    if short_leg is None:
        return PASS("no_eligible_short_leg", strategy="S5", candidate_id=candidate.id)

    debit = long_leg.mid - short_leg.mid
    spread_width = abs(long_leg.strike - short_leg.strike)
    if spread_width <= 0:
        return PASS("invalid_spread_width", strategy="S5", candidate_id=candidate.id)

    if debit / spread_width > ex["debit_to_width_max"]:
        return PASS("debit_too_expensive", strategy="S5", candidate_id=candidate.id)

    # Average spread % across both legs
    avg_spread_pct = (long_leg.spread_pct + short_leg.spread_pct) / 2.0
    if avg_spread_pct > ex["option_spread_pct_max"]:
        return PASS("option_spread_too_wide", strategy="S5", candidate_id=candidate.id)

    limit_price = debit * ex["limit_buffer"]
    vertical = VerticalOrder(
        underlying=candidate.ticker,
        long_leg=_to_option_leg(long_leg),
        short_leg=_to_option_leg(short_leg),
        side=side,
        qty=1,                                   # caller adjusts via sizing
        limit_price=limit_price,
        expiry=expiry,
        spread_width=spread_width,
        debit=debit,
    )
    return TRADE("S5", candidate_id=candidate.id, instrument=vertical)
