"""strategies/s5_gamma_reversal/execution/option_selector.py — Vertical leg picker.

Per spec §10.4 s5.execution + §10.5 build_s5_order:
  dte_min: 7
  dte_max: 14
  delta_min: 0.35
  delta_max: 0.45
  debit_to_width_max: 0.40
  option_spread_pct_max: 0.12
  limit_buffer: 1.02         (2% above mid)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Literal, Optional


@dataclass
class ContractRow:
    """Minimal option-chain row consumed by the selector."""
    symbol: str
    strike: float
    delta: float
    bid: float
    ask: float
    open_interest: int
    volume: int
    expiry: date
    side: Literal["call", "put"]

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return 1.0
        return (self.ask - self.bid) / self.mid


def choose_expiry(chain: Iterable[ContractRow],
                   today: date,
                   min_dte: int, max_dte: int) -> Optional[date]:
    """Pick the nearest expiry within [min_dte, max_dte]. None if nothing matches."""
    eligible = sorted({c.expiry for c in chain
                        if min_dte <= (c.expiry - today).days <= max_dte})
    return eligible[0] if eligible else None


def choose_long_leg(chain: Iterable[ContractRow], expiry: date,
                     side: Literal["call", "put"],
                     delta_min: float, delta_max: float
                     ) -> Optional[ContractRow]:
    """Pick the long leg whose |delta| is in [delta_min, delta_max]."""
    candidates = [
        c for c in chain
        if c.expiry == expiry and c.side == side
        and delta_min <= abs(c.delta) <= delta_max
    ]
    if not candidates:
        return None
    # Prefer the one with delta closest to (delta_min+delta_max)/2
    target = (delta_min + delta_max) / 2.0
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def choose_short_leg(chain: Iterable[ContractRow], long_leg: ContractRow,
                      target_width: float) -> Optional[ContractRow]:
    """Pick the short leg ~target_width away from the long leg."""
    if long_leg.side == "call":
        target_strike = long_leg.strike + target_width
    else:
        target_strike = long_leg.strike - target_width
    candidates = [
        c for c in chain
        if c.expiry == long_leg.expiry and c.side == long_leg.side
        and c.strike != long_leg.strike
        and ((long_leg.side == "call" and c.strike > long_leg.strike)
             or (long_leg.side == "put" and c.strike < long_leg.strike))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.strike - target_strike))
