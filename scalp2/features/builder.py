"""features/builder.py — Compose Features from raw bars + trades + UW data.

Spec SCALP-1. Computed at bar close only (SCALP-1.T2 — no look-ahead).
Hash stored in any decision logs (SCALP-1.T1 — stable across runs).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from features.datatypes import Features, GEXSnapshot, FlowRecord
from features.price_structure import (
    BarOHLC, atr_from_bars, vwap_from_bars,
    extension_from_vwap_atr, extension_from_prior_close_atr,
    failed_extension_atr, wick_pcts,
)
from features.volume_features import (
    push_volumes, volume_divergence_ratio, climax_vol_ratio,
)
from features.aggressor import (
    classify_trades, aggressor_recent_prior, aggressor_velocity,
    flip_strength,
)
from features.gamma_features import (
    distance_to_major_pos_gex_atr, gamma_flip_distance, gex_magnitude_rank,
)
from features.flow_features import (
    net_signed_premium, call_ask_pct, put_ask_pct, sweep_count, flow_flip,
    signed_flow_score,
)


def build_features(*,
                    ticker: str,
                    bar_idx: int,
                    bars: list[BarOHLC],
                    trades: list,
                    quotes: list,
                    gex_snapshot: Optional[GEXSnapshot],
                    flow_records: list[FlowRecord],
                    near_htf_level_atr: float = 1.0,
                    earnings_blackout: bool = False,
                    fomc_blackout: bool = False,
                    iv_percentile: float = 0.5,
                    front_iv_change: float = 0.0,
                    skew_slope: float = 0.0,
                    bid_ask_spread_pct: float = 0.0,
                    option_volume_5d_avg: float = 0.0,
                    option_open_interest: float = 0.0,
                    option_spread_pct: float = 0.0,
                    timestamp: Optional[datetime] = None) -> Features:
    """SCALP-1. Build per-bar Features from all input streams.

    Caller is responsible for slicing bars/trades/quotes/flow_records to
    decision-time only (no future data).
    """
    if not bars:
        raise ValueError("build_features requires at least one bar")

    last_bar = bars[-1]
    prior_bar = bars[-2] if len(bars) >= 2 else last_bar
    timestamp = timestamp or datetime.now(tz=timezone.utc)

    # ── Price structure ──
    atr = atr_from_bars(bars, period=14)
    vwap = vwap_from_bars(bars)
    ext_vwap = extension_from_vwap_atr(last_bar.c, vwap, atr)
    ext_prior = extension_from_prior_close_atr(last_bar.c, prior_bar.c, atr)
    failed_ext = failed_extension_atr(
        prior_extension_peak=ext_vwap, current_close=last_bar.c,
        vwap_at_peak=vwap, atr=atr, direction="long",
    ) if atr > 0 else 0.0
    pullback_break = last_bar.l < prior_bar.l
    wp, uw_pct, lw_pct = wick_pcts(last_bar)

    # ── Volume ──
    cur_v, pri_v = push_volumes(bars)
    vol_div = volume_divergence_ratio(cur_v, pri_v)
    climax_v = climax_vol_ratio(bars)

    # ── Aggressor (Lee-Ready or bar-direction proxy if no trades feed) ──
    if trades and quotes:
        classified = classify_trades(trades, quotes)
        cutoff_ep = timestamp.timestamp()
        agg_recent, agg_prior = aggressor_recent_prior(classified, cutoff_ep, window_seconds=300)
    else:
        # Backtest fallback — derive from bar close-vs-open sign over last 5 / 5-10 bars
        def _bar_dir_avg(b_window):
            if not b_window:
                return 0.0
            signs = [(1 if b.c > b.o else (-1 if b.c < b.o else 0)) for b in b_window]
            return sum(signs) / len(signs)

        agg_recent = _bar_dir_avg(bars[-5:])
        agg_prior = _bar_dir_avg(bars[-10:-5]) if len(bars) >= 10 else 0.0
    agg_vel = aggressor_velocity(agg_recent, agg_prior, dt_seconds=60.0)
    flip_str = flip_strength(agg_recent, agg_prior)

    # ── Gamma ──
    if gex_snapshot:
        spot = gex_snapshot.spot_price or last_bar.c
        dist_pos = distance_to_major_pos_gex_atr(spot, gex_snapshot.major_pos_gex_strike, atr)
        gflip = gamma_flip_distance(spot, gex_snapshot.gamma_flip, atr)
        gex_age = gex_snapshot.age_min
        gex_strikes_count = len(gex_snapshot.gex_by_strike)
        gex_mag = gex_magnitude_rank(gex_snapshot, gex_snapshot.major_pos_gex_strike) if gex_strikes_count else 0.0
        gex_dte = 7
    else:
        dist_pos = 99.0
        gflip = 99.0
        gex_age = 999
        gex_mag = 0.0
        gex_dte = 0

    # ── Options flow ──
    net_5m = net_signed_premium(flow_records, timestamp, window_minutes=5) if flow_records else 0.0
    net_30m = net_signed_premium(flow_records, timestamp, window_minutes=30) if flow_records else 0.0
    cap = call_ask_pct(flow_records, timestamp, window_minutes=5) if flow_records else 0.0
    pap = put_ask_pct(flow_records, timestamp, window_minutes=5) if flow_records else 0.0
    sweeps = sweep_count(flow_records, timestamp, window_minutes=5) if flow_records else 0
    flip = flow_flip(flow_records, timestamp) if flow_records else False
    sfs = signed_flow_score(flow_records, timestamp, window_minutes=30) if flow_records else 0.0

    return Features(
        ticker=ticker, timestamp=timestamp, bar_idx=bar_idx,
        open=last_bar.o, high=last_bar.h, low=last_bar.l, close=last_bar.c,
        extension_from_vwap_atr=ext_vwap,
        extension_from_prior_close_atr=ext_prior,
        failed_extension_atr=failed_ext,
        pullback_break=pullback_break,
        wick_pct=wp, upper_wick_pct=uw_pct, lower_wick_pct=lw_pct,
        volume=last_bar.v,
        volume_divergence_ratio=vol_div,
        current_push_vol=cur_v, prior_push_vol=pri_v,
        climax_vol_ratio=climax_v,
        aggressor_recent=agg_recent, aggressor_prior=agg_prior,
        aggressor_velocity=agg_vel, flip_strength=flip_str,
        distance_to_major_pos_gex_atr=dist_pos,
        distance_to_pos_gex_atr=dist_pos,
        gex_magnitude_rank=gex_mag,
        gamma_flip_distance=gflip,
        strike_oi_rank=0.0,
        major_gex_strike_dte=gex_dte,
        gex_snapshot_age_min=gex_age,
        net_signed_premium_5m=net_5m, net_signed_premium_30m=net_30m,
        call_ask_pct=cap, put_ask_pct=pap,
        sweep_count=sweeps, flow_flip=flip,
        signed_flow_score=sfs,
        near_htf_level_atr=near_htf_level_atr,
        earnings_blackout=earnings_blackout, fomc_blackout=fomc_blackout,
        iv_percentile=iv_percentile, front_iv_change=front_iv_change,
        skew_slope=skew_slope,
        bid_ask_spread_pct=bid_ask_spread_pct,
        option_volume_5d_avg=option_volume_5d_avg,
        option_open_interest=option_open_interest,
        option_spread_pct=option_spread_pct,
    )
