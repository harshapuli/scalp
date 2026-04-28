"""features/price_structure.py — Price-structure features. SCALP-1.1.

Per spec §10.2 Features dataclass + §B4 reference:
  extension_from_vwap_atr            — (close - vwap) / atr, signed
  extension_from_prior_close_atr     — (close - prior_close) / atr, signed
  failed_extension_atr               — how far retraced from extension peak
  pullback_break                     — last pullback's low broken?
  wick_pct, upper_wick_pct, lower_wick_pct — wick proportions of range

All inputs are bar OHLC + computed VWAP + ATR. No external dependencies.
Bar-close-only computation; no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BarOHLC:
    """Minimal bar-shaped object the price-structure features consume."""
    o: float; h: float; l: float; c: float
    v: float = 0.0


def extension_from_vwap_atr(close: float, vwap: float, atr: float) -> float:
    """Signed extension from VWAP, ATR-normalized."""
    if atr <= 0:
        return 0.0
    return (close - vwap) / atr


def extension_from_prior_close_atr(close: float, prior_close: float, atr: float) -> float:
    """Signed extension from prior close, ATR-normalized."""
    if atr <= 0:
        return 0.0
    return (close - prior_close) / atr


def wick_pcts(bar: BarOHLC) -> tuple[float, float, float]:
    """Returns (wick_pct, upper_wick_pct, lower_wick_pct) — each in [0, 1].

    wick_pct = max(upper, lower) / range.
    """
    rng = bar.h - bar.l
    if rng <= 0:
        return 0.0, 0.0, 0.0
    body_top = max(bar.o, bar.c)
    body_bot = min(bar.o, bar.c)
    upper = (bar.h - body_top) / rng
    lower = (body_bot - bar.l) / rng
    upper = max(0.0, min(1.0, upper))
    lower = max(0.0, min(1.0, lower))
    return max(upper, lower), upper, lower


def failed_extension_atr(prior_extension_peak: float,
                          current_close: float,
                          vwap_at_peak: float,
                          atr: float,
                          direction: str = "long") -> float:
    """How far has price retraced from the prior extension peak?

    direction='long': peak was an upward extension; failure measured downward
    direction='short': peak was a downward extension; failure measured upward

    Returns retrace distance in ATR units (signed positive when retracing
    against the peak direction — i.e., failed extension growing).
    """
    if atr <= 0 or prior_extension_peak == 0:
        return 0.0
    current_extension = (current_close - vwap_at_peak) / atr
    if direction == "long":
        return prior_extension_peak - current_extension
    else:
        return current_extension - prior_extension_peak


def pullback_break(prior_pullback_low: float, current_low: float) -> bool:
    """Long context: did current bar break below the last pullback's low?
    Use mirror-image for short context (caller swaps args).
    """
    return current_low < prior_pullback_low


def vwap_from_bars(bars: list[BarOHLC]) -> float:
    """Session VWAP from a list of bars (typical price * volume)."""
    if not bars:
        return 0.0
    pv = 0.0
    v = 0.0
    for b in bars:
        typical = (b.h + b.l + b.c) / 3.0
        pv += typical * b.v
        v += b.v
    if v <= 0:
        return bars[-1].c
    return pv / v


def atr_from_bars(bars: list[BarOHLC], period: int = 14) -> float:
    """True-range ATR over the last `period` bars.

    Spec S5-30.1: this is decision-time ATR. Caller is responsible for
    passing only bars up to (not including) the decision bar.
    """
    if len(bars) < 2:
        return 0.0
    period = min(period, len(bars) - 1)
    trs = []
    for i in range(1, len(bars)):
        b = bars[i]
        prev_c = bars[i - 1].c
        tr = max(
            b.h - b.l,
            abs(b.h - prev_c),
            abs(b.l - prev_c),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / max(1, len(trs))
    return sum(trs[-period:]) / period


if __name__ == "__main__":
    bars = [BarOHLC(o=100, h=101, l=99.5, c=100.5, v=1000) for _ in range(15)]
    bars.append(BarOHLC(o=100.5, h=102.5, l=100.4, c=102.0, v=2000))
    atr = atr_from_bars(bars)
    vwap = vwap_from_bars(bars)
    print(f"[price_structure] atr={atr:.3f} vwap={vwap:.3f}")
    last = bars[-1]
    ext = extension_from_vwap_atr(last.c, vwap, atr)
    print(f"[price_structure] extension_from_vwap_atr={ext:+.3f}")
    wp, uw, lw = wick_pcts(last)
    print(f"[price_structure] wick_pct={wp:.3f} upper={uw:.3f} lower={lw:.3f}")
    print("[price_structure] OK")
