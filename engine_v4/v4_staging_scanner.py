"""
V4 STAGING SCANNER (v7 — empirically weighted from week-long backtest)

For each ticker in the latest snapshot universe, computes a STAGING SCORE
that flags pre-break setups. Backtested across 498 ticker-day observations:

  TOP 20 by score:  8/20 (40%) had +5%+ breakout in next 3 days
  Average forward 3d max:  +5.19%
  Average forward 1d:      +3.96%

The score is empirically weighted by each signal's measured lift on this
week's data:
  put/call ≥ 1.5     1.87x  (capitulation reversal)
  beta ≥ 2.0         1.85x  (high-vol coiled)
  Tech sector        1.70x
  IV ≥ 80            1.50x
  call_ask ≥ 0.55    1.38x
  IV rising 5d       1.36x
  cum_3d 0-10%       1.95x  (trending but not extended)

Hard gates:
  - Skip if had +5% day in last 3 sessions (already broke)
  - Skip if today's move is +7%+ (already extended today)

Output: v4_staging_signals.json — ranked list of setups for next-day positioning.

Run via: python3 v4_staging_scanner.py
Or schedule daily after the snapshot completes (~12:50 PM PT).
"""
import os, json, glob, sys
from datetime import datetime, timezone, date
from collections import defaultdict
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, 'v4_staging_signals.json')


def _safe_get(d, *keys, default=None):
    """Safe nested dict access."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def extract_features(ticker, snap_today_data, snap_yest_data, snap_2d_ago_data, daily_bars):
    """Extract every feature needed for the score from cached snapshot data."""
    if not snap_today_data: return None
    info = _safe_get(snap_today_data, 'info', 'data') or {}
    ov = (_safe_get(snap_today_data, 'options_volume', 'data') or [{}])[0]
    iv_arr = _safe_get(snap_today_data, 'iv_rank', 'data') or []
    dp_today = _safe_get(snap_today_data, 'darkpool', 'data') or []
    npt = _safe_get(snap_today_data, 'net_prem_ticks', 'data') or []
    fps = _safe_get(snap_today_data, 'flow_per_strike') or []
    if isinstance(fps, dict): fps = fps.get('data') or []

    # Price action features (from daily_bars input)
    if not daily_bars or len(daily_bars) < 4: return None
    closes = [b['c'] for b in daily_bars]
    highs = [b['h'] for b in daily_bars]
    lows = [b['l'] for b in daily_bars]
    today_close = closes[-1]
    today_open = daily_bars[-1].get('o', today_close)
    today_pct = (today_close - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
    cum_3d = sum((closes[i] - closes[i-1]) / closes[i-1] * 100
                 for i in range(max(1, len(closes)-3), len(closes)) if closes[i-1])
    last3_pcts = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                  for i in range(max(1, len(closes)-3), len(closes)) if closes[i-1]]
    had_recent_5pct = any(p >= 5 for p in last3_pcts)

    # 5-day high vs current
    last5 = daily_bars[-5:] if len(daily_bars) >= 5 else daily_bars
    high_5d = max(b['h'] for b in last5)
    pct_below_5d_high = (high_5d - today_close) / today_close * 100 if today_close else 0

    # Options flow
    call_vol = float(ov.get('call_volume', 0) or 0)
    put_vol = float(ov.get('put_volume', 0) or 0)
    avg_7d_call = float(ov.get('avg_7_day_call_volume', 1) or 1)
    call_ask = float(ov.get('call_volume_ask_side', 0) or 0)
    bull = float(ov.get('bullish_premium', 0) or 0) / 1e6
    bear = float(ov.get('bearish_premium', 0) or 0) / 1e6

    # IV trajectory (5-day delta)
    iv_today = float(iv_arr[-1].get('iv_rank_1y', 0) or 0) if iv_arr else 0
    iv_5d_ago = float(iv_arr[0].get('iv_rank_1y', 0) or 0) if iv_arr else iv_today
    iv_change_5d = iv_today - iv_5d_ago

    # Darkpool
    dp_prem_m = sum(float(d.get('premium', 0) or 0) for d in dp_today) / 1e6

    # Late-hour call buying (last 12 ticks ≈ last hour)
    last_hr_call_prem = 0
    if len(npt) >= 24:
        for t in npt[-12:]:
            last_hr_call_prem += float(t.get('net_call_premium', 0) or 0)
    last_hr_call_prem_m = last_hr_call_prem / 1e6

    # OTM call premium concentration (strikes 2-10% above current)
    otm_call_prem_m = 0
    if fps and today_close > 0:
        for f in fps:
            try:
                strike = float(f.get('strike', 0) or 0)
                if 1.02 * today_close <= strike <= 1.10 * today_close:
                    otm_call_prem_m += (float(f.get('call_premium_ask_side', 0) or 0)
                                        - float(f.get('call_premium_bid_side', 0) or 0))
            except: pass
    otm_call_prem_m /= 1e6

    # Beta + sector + mcap
    beta = float(info.get('beta', 0) or 0)
    sector = info.get('sector') or '—'
    mcap_b = float(info.get('marketcap', 0) or 0) / 1e9

    return {
        'ticker': ticker,
        'sector': sector,
        'beta': beta,
        'mcap_b': mcap_b,
        'last_close': today_close,
        'today_pct': today_pct,
        'cum_3d': cum_3d,
        'pct_below_5d_high': pct_below_5d_high,
        'had_recent_5pct': had_recent_5pct,
        'iv_today': iv_today,
        'iv_change_5d': iv_change_5d,
        'dp_prem_m': dp_prem_m,
        'put_call_ratio': put_vol / max(call_vol, 1),
        'call_ask_share': call_ask / max(call_vol, 1),
        'last_hr_call_prem_m': last_hr_call_prem_m,
        'otm_call_prem_m': otm_call_prem_m,
        'bull_m': bull,
        'bear_m': bear,
        'total_opt_prem_m': bull + bear,  # v8 — active-options-name signal
    }


def staging_score_v7(f, uw_signals=None):
    """V7 — empirically weighted by signal lift from week-long backtest.
    Returns 0 (skip) or positive score. Top picks are score >= 290.

    uw_signals (optional): per-ticker dict from v4_uw_signals_<DATE>.json
                           providing iv_rv_ratio, rv_pct_60d, gamma_compression.
                           If None or fields missing, those bonuses skip cleanly.
    """
    if not f: return 0
    # HARD GATES
    if f.get('had_recent_5pct'): return 0  # already broke recently
    if (f.get('today_pct') or 0) >= 7: return 0  # already up huge today

    s = 0
    # Components weighted ×100 of measured single-signal lift
    if (f.get('dp_prem_m') or 0) >= 100: s += 19      # 1.19x lift
    if (f.get('iv_today') or 0) >= 80: s += 50         # 1.50x
    if (f.get('put_call_ratio') or 0) >= 1.5: s += 87  # 1.87x ← biggest single signal
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38  # 1.38x
    if (f.get('beta') or 0) >= 2.0: s += 85            # 1.85x
    if (f.get('iv_change_5d') or 0) >= 10: s += 36     # 1.36x
    if f.get('sector') == 'Technology': s += 70        # 1.70x discount
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 95                       # best-lift signal
    # Pre-break sweet spot
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5: s += 30
    # Bonus for late-day call buying + OTM positioning (the n=6 monster signal)
    if (f.get('last_hr_call_prem_m') or 0) >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 50
    elif (f.get('last_hr_call_prem_m') or 0) >= 5:
        s += 12
    # Anti-signals
    if (f.get('iv_today') or 0) <= 30: s -= 69         # 0.31x lift
    if (f.get('today_pct') or 0) >= 5: s -= 15         # extending too fast

    # NEW (added 2026-04-25 from clean 5,983-obs framework backtest):
    # iv_rv_ratio < 0.85 → 1.32x lift on prebreakout setups (cheapest options)
    # rv_pct_60d > 0.7  → 1.17x lift (high realized vol regime)
    # gamma_compression → 1.10x lift (slow gamma squeeze precedes break)
    if uw_signals:
        if uw_signals.get('vr_iv_cheap'): s += 32              # 1.32x lift
        if uw_signals.get('vr_rv_high_regime'): s += 17        # 1.17x lift
        if uw_signals.get('ge_gamma_compression'): s += 10     # 1.10x lift

        # PULLED 2026-04-25 — archetype bonuses tested INVALID per framework:
        #   is_coiled        → 0.36x lift (ANTI-predictive!) — coiled stocks usually stay dead
        #   is_sympathy      → 0.83x (no edge over base rate)
        #   is_elite combo   → 0.00x (n=50, 0 wins)
        # Smart money still untested (no historical UW per-ticker data).
        # Keeping fields for display only — NOT scoring.
        # if uw_signals.get('is_smart_money'): s += 30  # untestable for now, hold
    return s


# Backtested BUY threshold. Top-20 by v7 score hit 40% (5%+ in 3d).
# Score >= 300 was the cleanest cut: ~37% hit rate with very low false-positive rate.
V7_BUY_THRESHOLD = 300

# v8 has a NARROWER score range (~370 max realistic vs ~600 for v7) because
# we removed +87/+95/+85 weights that didn't show winner-z separation.
# Calibrated against 422-obs Apr 22-29 window: 200 yields ~30 picks (similar
# n to v7@300), and is approximately at the inflection of score-vs-edge curve.
V8_BUY_THRESHOLD = 200
# BUY_THRESHOLD is selected AFTER SCORE_FN_VERSION is defined (see below
# the v8 score functions) — Python parses top-to-bottom and SCORE_FN_VERSION
# isn't a constant we can evaluate this early.


# ─────────────────────────────────────────────────────────────────────────
# V8 — re-weighted from 34-winner analysis (Apr 22-29, 422 ticker-days)
#
# Why v8 exists:
#   v7 lift weights were measured on n=498 ticker-days from a single week,
#   where n_winners was small. Expanded analysis to 422 obs / 34 winners
#   showed several v7 weights have z=0 separation between winners & losers
#   (they don't actually predict). v8 reweights based on z-score evidence:
#
#     SIGNAL                        v7 wt   z-score (W vs L)   v8 wt
#     ─────────────────────────────────────────────────────────────────
#     last_hr_call_prem_m ≥ 5        +12     +0.55 STRONG       +50  ★★ promoted
#     iv_today ≥ 80                  +50     +0.40 mod          +50  =
#     call_ask_share ≥ 0.55          +38     +0.32 mod          +38  = (also new ≥0.45 tier)
#     today_pct already up 0.5-3%      0     +0.25              +25  ★ NEW
#     mcap_b ≥ 100                     0     +0.25              +25  ★ NEW
#     beta 2.0-2.5 (band, not ≥2)    +85     +0.25 sweet spot   +60  ↓ band only
#     beta < 0.5 (defensive)           0     +0.25              +30  ★ NEW
#     beta ≥ 2.5 (dead zone)         +85       0.00            -30  ★ INVERTED
#     sector Healthcare                0     1.24× lift         +30  ★ NEW
#     sector Energy                    0     2.07× lift (n=12)  +30  ★ NEW (small-n caveat)
#     sector Technology              +70     1.68× lift         +30  ↓ de-concentrated
#     cum_3d 0-10%                   +95     +0.10 weak         +50  ↓ over-weighted in v7
#     total_opt_prem_m ≥ 100 (active) 0     +0.35 mod          +25  ★ NEW
#     put_call_ratio ≥ 1.5           +87     -0.11 noise         0  ★★ REMOVED
#     dp_prem_m ≥ 100                +19     -0.04 noise         0  ★★ REMOVED
#     iv_change_5d ≥ 10              +36      0.00 noise         0  ★★ REMOVED
#     pct_below_5d_high 1-5%         +30     -0.10 noise         0  ★★ REMOVED
#
# Hard gates: kept as-is (had_recent_5pct, today_pct ≥ 7).
#
# Risk acknowledged: n=34 winners is low. Use SCORE_FN_VERSION = 'v7' to
# revert quickly. weekly compare script (v4_formula_weekly_compare.py)
# tracks v7 vs v8 hit rates as data accumulates.
# ─────────────────────────────────────────────────────────────────────────

def staging_score_v8(f, uw_signals=None):
    """V8 — winner-distribution-driven weights.

    Top-end (max-realistic) score is ~370. BUY threshold reused at 300.
    """
    if not f: return 0
    # HARD GATES (same as v7)
    if f.get('had_recent_5pct'): return 0
    if (f.get('today_pct') or 0) >= 7: return 0

    s = 0
    # ---- Strong signals (high winner-z) ----
    last_hr = f.get('last_hr_call_prem_m') or 0
    if last_hr >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 60                                            # combo, was +50
    elif last_hr >= 5:
        s += 50                                            # was +12 (boosted from z=+0.55)
    elif last_hr >= 2:
        s += 25                                            # NEW lower-tier
    if (f.get('iv_today') or 0) >= 80: s += 50             # 50, z=+0.40
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38     # was 38, z=+0.32
    elif (f.get('call_ask_share') or 0) >= 0.45: s += 15   # NEW lower-tier
    if 0.5 <= (f.get('today_pct') or 0) <= 3.0: s += 25    # NEW (in-the-move not exhausted)
    if (f.get('mcap_b') or 0) >= 100: s += 25              # NEW (megacap edge)
    if (f.get('total_opt_prem_m') or 0) >= 100: s += 25    # NEW (active-options name)

    # ---- Beta as a BAND, not threshold ----
    beta = f.get('beta') or 0
    if 2.0 <= beta < 2.5: s += 60                          # sweet spot (2.73× lift)
    elif 0 <= beta < 0.5: s += 30                          # defensive winners
    elif beta >= 2.5: s -= 30                              # dead zone penalty

    # ---- Sectors with measured lift ≥ 1.2× ----
    sector = f.get('sector') or ''
    if sector == 'Technology': s += 30                     # 1.68× lift, was +70
    elif sector == 'Healthcare': s += 30                   # 1.24× lift, NEW
    elif sector == 'Energy': s += 30                       # 2.07× lift (n=12), NEW

    # ---- Multi-day setup (reduced weight) ----
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 50                          # was +95, z only +0.10

    # ---- v7 signals REMOVED in v8 (z ≈ 0):
    # put_call_ratio ≥ 1.5  (was +87 — noise)
    # dp_prem_m ≥ 100       (was +19 — noise)
    # iv_change_5d ≥ 10     (was +36 — noise)
    # pct_below_5d_high 1-5% (was +30 — noise)

    # ---- Anti-signals (kept) ----
    if (f.get('iv_today') or 0) <= 30: s -= 50             # was -69
    if (f.get('today_pct') or 0) >= 5: s -= 15

    # ---- UW intraday signals (unchanged) ----
    if uw_signals:
        if uw_signals.get('vr_iv_cheap'): s += 32
        if uw_signals.get('vr_rv_high_regime'): s += 17
        if uw_signals.get('ge_gamma_compression'): s += 10
    return s


def staging_score_v8_put(f, uw_signals=None):
    """V8 PUT — bearish mirror, same reweighting philosophy.
    Note: PUT side has even less validation data; treat as experimental.
    """
    if not f: return 0
    if (f.get('today_pct') or 0) <= -7: return 0
    if (f.get('cum_3d') or 0) <= -5: return 0

    s = 0
    last_hr_put = f.get('last_hr_put_prem_m') or 0
    last_hr_call = f.get('last_hr_call_prem_m') or 0
    if last_hr_put >= 5: s += 50                           # mirror of bullish boost
    elif last_hr_put >= 2: s += 25
    if (f.get('iv_today') or 0) >= 80: s += 50
    # call_ask_share inverted: low = aggressive sellers
    if (f.get('call_ask_share') or 0) <= 0.40: s += 38
    elif (f.get('call_ask_share') or 0) <= 0.45: s += 15
    if -3.0 <= (f.get('today_pct') or 0) <= -0.5: s += 25  # in-the-down-move not exhausted
    if (f.get('mcap_b') or 0) >= 100: s += 25
    if (f.get('total_opt_prem_m') or 0) >= 100: s += 25

    beta = f.get('beta') or 0
    if 2.0 <= beta < 2.5: s += 60
    elif 0 <= beta < 0.5: s += 30
    elif beta >= 2.5: s -= 30

    sector = f.get('sector') or ''
    if sector == 'Technology': s += 30
    elif sector == 'Healthcare': s += 30
    elif sector == 'Energy': s += 30

    cum_3d = f.get('cum_3d') or 0
    if -3 <= cum_3d <= 0: s += 50                          # multi-day decline sweet spot

    # Removed in v8 (same reasoning):
    # put_call_ratio extremes (was +87)
    # dp_prem_m ≥ 100 (was +19)
    # iv_change_5d ≥ 10 (was +36)
    # pct_below 0-5% (was +30)

    if (f.get('iv_today') or 0) <= 30: s -= 50
    if (f.get('today_pct') or 0) <= -5: s -= 15

    if uw_signals:
        if uw_signals.get('vr_iv_cheap'): s += 32
        if uw_signals.get('vr_rv_high_regime'): s += 17
        if uw_signals.get('ge_gamma_compression'): s += 10
    return s


# ─── Active scorer config ─────────────────────────────────────────────
# Flip to 'v7' to revert. Output JSON includes BOTH scores so we can
# track v7-vs-v8 divergence as data grows.
SCORE_FN_VERSION = 'v8'
_SCORE_FN      = staging_score_v8     if SCORE_FN_VERSION == 'v8' else staging_score_v7
_SCORE_FN_PUT  = staging_score_v8_put if SCORE_FN_VERSION == 'v8' else staging_score_v7_put
BUY_THRESHOLD     = V8_BUY_THRESHOLD if SCORE_FN_VERSION == 'v8' else V7_BUY_THRESHOLD
PUT_BUY_THRESHOLD = V8_BUY_THRESHOLD if SCORE_FN_VERSION == 'v8' else V7_BUY_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────
# DIRECTIONAL GATE (v8+)
#
# Why: structural signals (beta band, Tech sector, mcap, UW) are direction-
# agnostic and credit BOTH the call-side and put-side scorers equally.
# That means a high-beta tech megacap collects ~+175 baseline points on
# either side just for being a high-beta tech megacap. With v8's narrower
# threshold (200), a single small directional signal can push BOTH sides
# above threshold — INTC dual-fired today with score=224 / put_score=212
# despite today_pct=-3.35% (clearly bearish).
#
# Fix: hard-gate by price direction. The ticker has to actually be moving
# in the side's direction. Bands chosen so neutral-day picks (|today_pct|
# < 1%) can still score either side and let other signals decide.
# ─────────────────────────────────────────────────────────────────────────
DIRECTIONAL_GATE_PCT = 1.0  # |today_pct| ≤ this → both sides allowed; outside → only the agreeing side

def _apply_directional_gate(f, call_score, put_score):
    """Zero out the side disagreeing with today's price direction.

    Returns (call_score, put_score) — possibly zeroed.
    Conservative: only forces the issue when today_pct has CLEAR direction.
    """
    if not f: return call_score, put_score
    today_pct = f.get('today_pct') or 0
    if today_pct >= DIRECTIONAL_GATE_PCT:
        # clearly up today — PUT side has no business firing
        put_score = 0
    elif today_pct <= -DIRECTIONAL_GATE_PCT:
        # clearly down today — CALL side has no business firing
        call_score = 0
    # else: in -1.0 < today_pct < +1.0 → leave both, let other signals decide
    return call_score, put_score


def staging_score_v7_put(f, uw_signals=None):
    """V7 PUT — mirror of bullish V7 for break-down detection.
    Sprint 3.9. Identifies institutional distribution + downside setup.

    HARD GATES:
      - already crashed today (today_pct ≤ -7) → return 0
      - already broke down recently (had_recent_drop) → return 0 (proxy via cum_3d ≤ -5)
    """
    if not f: return 0
    # HARD GATES
    if (f.get('today_pct') or 0) <= -7: return 0     # already collapsed
    if (f.get('cum_3d') or 0) <= -5: return 0        # already broke down

    s = 0
    pcr = f.get('put_call_ratio') or 0
    iv_today = f.get('iv_today') or 0
    call_ask = f.get('call_ask_share') or 0.5
    beta = f.get('beta') or 0
    sector = f.get('sector') or ''
    iv_chg = f.get('iv_change_5d') or 0
    dp = f.get('dp_prem_m') or 0
    cum_3d = f.get('cum_3d') or 0
    pct_below = f.get('pct_below_5d_high') or 0
    today_pct = f.get('today_pct') or 0
    last_hr_call = f.get('last_hr_call_prem_m') or 0
    last_hr_put = f.get('last_hr_put_prem_m') or 0  # may not exist; safely 0

    # Mirror of bullish components (with inverted thresholds where relevant)
    if dp >= 100: s += 19           # DP heavy = institutions positioning either side
    if iv_today >= 80: s += 50       # IV elevated = market expecting move
    if pcr <= 0.5: s += 87           # Heavy CALL buying often marks TOPS (mirror of capit)
    if call_ask <= 0.45: s += 38     # Calls hit at BID = aggressive sellers
    if beta >= 2.0: s += 85          # High beta = bigger DROPS too
    if iv_chg >= 10: s += 36         # IV expansion = move incoming
    if sector == 'Technology': s += 70  # Tech = leveraged moves both ways
    # Multi-day decline sweet spot (cum_3d -3 to 0 = controlled distribution)
    if -3 <= cum_3d <= 0: s += 95
    # Pre-break sweet spot — close to recent HIGH (about to roll over)
    if 0 <= pct_below <= 5: s += 30  # tight to high = ready to break down
    # Late-day PUT buying
    if last_hr_put >= 5: s += 12
    # Anti-signals
    if iv_today <= 30: s -= 69       # no expected move
    if today_pct <= -5: s -= 15      # extending down too fast = exhaustion bounce risk

    # UW intraday signals (same as bullish — these are direction-agnostic context)
    if uw_signals:
        if uw_signals.get('vr_iv_cheap'): s += 32
        if uw_signals.get('vr_rv_high_regime'): s += 17
        if uw_signals.get('ge_gamma_compression'): s += 10

    return s


# PUT_BUY_THRESHOLD is set higher up next to BUY_THRESHOLD (after
# SCORE_FN_VERSION is defined). Leaving this stub so a grep still finds it.
# legacy: PUT_BUY_THRESHOLD = 300


def classify_pattern_put(f):
    """PUT-side pattern label — break-down detector. Sprint 3.9."""
    if not f: return ('Mixed', '')
    cum_3d = f.get('cum_3d') or 0
    pct_below = f.get('pct_below_5d_high') or 0
    pcr = f.get('put_call_ratio') or 0
    iv_today = f.get('iv_today') or 0
    iv_chg = f.get('iv_change_5d') or 0
    beta = f.get('beta') or 0
    sector = f.get('sector') or ''
    dp = f.get('dp_prem_m') or 0
    last_hr_put = f.get('last_hr_put_prem_m') or 0
    today_pct = f.get('today_pct') or 0

    # 1. Late-day institutional PUT buying
    if last_hr_put >= 5:
        return ('Late-day put buying',
                f'${last_hr_put:.0f}M put premium in final hour — institutional hedging/short')

    # 2. Distribution top — call exuberance often marks the high
    if pcr <= 0.4:
        return ('Distribution top',
                f'put/call {pcr:.2f} — extreme call buying, contrarian short signal')

    # 3. Roll-over setup — tight to high, no further upside
    if 0 <= pct_below <= 3 and -2 <= cum_3d <= 1:
        return ('Roll-over setup',
                f'-{pct_below:.1f}% from high, flat 3d — ready to break down')

    # 4. Multi-day decline — controlled distribution
    if -10 <= cum_3d < -3:
        return ('Multi-day decline',
                f'{cum_3d:.1f}% over 3 sessions, controlled distribution')

    # 5. IV ramp at high level
    if iv_chg >= 10 and iv_today >= 80:
        return ('IV expansion',
                f'IV {iv_today:.0f} (+{iv_chg:.0f} 5d) — vol pricing in down move')

    # 6. High-beta tech distribution
    if beta >= 2.0 and sector == 'Technology' and dp >= 100:
        return ('High-beta tech distribution',
                f'β{beta:.1f} chip name, DP ${dp:.0f}M outflow')

    # 7. Heavy DP outflow
    if dp >= 200:
        return ('Heavy DP outflow',
                f'${dp:.0f}M DP — institutions distributing')

    # Fallbacks
    if iv_chg >= 10:
        return ('IV expansion', f'IV +{iv_chg:.0f} over 5d')
    if beta >= 2.0:
        return ('High-beta swing-down', f'β{beta:.1f}')
    return ('Mixed setup', f'multi-factor — score {f.get("score", 0)}')


def classify_pattern(f):
    """Pick the SINGLE dominant pattern label for this setup.
    Order matters — strongest/rarest signal wins.
    Returns (pattern_name, one_line_why).
    """
    if not f: return ('Mixed', '')
    cum_3d = f.get('cum_3d') or 0
    pct_below = f.get('pct_below_5d_high') or 0
    pcr = f.get('put_call_ratio') or 0
    iv_today = f.get('iv_today') or 0
    iv_chg = f.get('iv_change_5d') or 0
    beta = f.get('beta') or 0
    sector = f.get('sector') or ''
    dp = f.get('dp_prem_m') or 0
    last_hr = f.get('last_hr_call_prem_m') or 0
    otm = f.get('otm_call_prem_m') or 0
    today_pct = f.get('today_pct') or 0

    # 1. Late-day institutional flow (n=6 monster signal in backtest)
    if last_hr >= 5 and otm >= 1:
        return ('Late-day institutional flow',
                f'${last_hr:.0f}M call premium in final hour, OTM positioning ${otm:.0f}M')

    # 2. Capitulation rebound — heavy puts often mark turns
    if pcr >= 1.5:
        return ('Capitulation rebound',
                f'put/call {pcr:.1f} — oversold, smart money positioning')

    # 3. Coiled spring — tight to recent high, not extended
    if 1 <= pct_below <= 5 and 0 <= cum_3d <= 3:
        return ('Coiled spring',
                f'-{pct_below:.1f}% from 5d high, only +{cum_3d:.1f}% over 3d — taut')

    # 4. Multi-day grind — steady accumulation 3-10% over 3d
    if 3 < cum_3d <= 10:
        return ('Multi-day grind',
                f'+{cum_3d:.1f}% over 3 sessions, steady accumulation')

    # 5. IV ramp — vol expanding fast at high level
    if iv_chg >= 10 and iv_today >= 80:
        return ('IV ramp',
                f'IV {iv_today:.0f} (+{iv_chg:.0f} over 5d) — market expecting move')

    # 6. High-beta tech setup — classic chip stock momentum profile
    if beta >= 2.0 and sector == 'Technology' and dp >= 100:
        return ('High-beta tech setup',
                f'β{beta:.1f} chip name, DP ${dp:.0f}M')

    # 7. Heavy DP flow — institutional positioning, no other clear signature
    if dp >= 200:
        return ('Heavy DP flow',
                f'${dp:.0f}M dark pool premium — institutions building')

    # 8. Single-signal fallbacks
    if iv_chg >= 10:
        return ('IV expansion', f'IV +{iv_chg:.0f} over 5d')
    if beta >= 2.0:
        return ('High-beta swing', f'β{beta:.1f}')

    return ('Mixed setup', f'multi-factor — score {f.get("score", 0)}')


def signal_breakdown(f):
    """Human-readable list of what fired for this ticker."""
    if not f: return []
    bits = []
    if (f.get('cum_3d') or 0) >= 0 and (f.get('cum_3d') or 0) <= 10:
        bits.append(f"trending +{f['cum_3d']:.1f}% (3d)")
    if (f.get('beta') or 0) >= 2.0:
        bits.append(f"β {f['beta']:.1f}")
    if f.get('sector') == 'Technology':
        bits.append("Tech")
    if (f.get('dp_prem_m') or 0) >= 100:
        bits.append(f"DP ${f['dp_prem_m']:.0f}M")
    if (f.get('iv_today') or 0) >= 80:
        bits.append(f"IV {f['iv_today']:.0f}")
    if (f.get('iv_change_5d') or 0) >= 10:
        bits.append(f"IV +{f['iv_change_5d']:.0f} 5d")
    if (f.get('put_call_ratio') or 0) >= 1.5:
        bits.append(f"P/C {f['put_call_ratio']:.1f} (capit)")
    if (f.get('call_ask_share') or 0) >= 0.55:
        bits.append(f"call_ask {int(f['call_ask_share']*100)}%")
    if (f.get('last_hr_call_prem_m') or 0) >= 5:
        bits.append(f"late-hr buy ${f['last_hr_call_prem_m']:.0f}M")
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5:
        bits.append(f"-{f['pct_below_5d_high']:.1f}% from 5d hi")
    return bits


def load_uw_signals():
    """Load latest v4_uw_signals_<DATE>.json if available — graceful when missing."""
    paths = sorted(glob.glob(os.path.join(HERE, 'v4_uw_signals_*.json')))
    if not paths: return {}
    try:
        with open(paths[-1]) as f: data = json.load(f)
        per_ticker = data.get('per_ticker') or {}
        print(f"loaded UW signals: {len(per_ticker)} tickers from {os.path.basename(paths[-1])}", file=sys.stderr)
        return per_ticker
    except Exception as e:
        print(f"could not load UW signals: {e}", file=sys.stderr)
        return {}


def main():
    # Find latest 3 snapshots
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    if len(snap_paths) < 1:
        print("No snapshot files found", file=sys.stderr)
        return
    snaps_loaded = []
    for p in snap_paths[-3:]:
        try:
            with open(p) as f: snaps_loaded.append((os.path.basename(p), json.load(f)))
        except: pass
    if not snaps_loaded:
        print("Could not load snapshots", file=sys.stderr)
        return

    today_name, today_snap = snaps_loaded[-1]
    yest_snap = snaps_loaded[-2][1] if len(snaps_loaded) >= 2 else {}
    twoago_snap = snaps_loaded[-3][1] if len(snaps_loaded) >= 3 else {}
    today_data = today_snap.get('data', {})
    yest_data = yest_snap.get('data', {})
    twoago_data = twoago_snap.get('data', {})

    print(f"Scanning {len(today_data)} tickers from snapshot {today_name}", file=sys.stderr)

    # Build chronologically-sorted unique daily bars per ticker.
    #
    # Bug fix (Apr 29 2026): the prior version appended each snapshot's
    # dailyBar AND prevDailyBar without deduplication. Because prevDailyBar
    # of snap_N == dailyBar of snap_(N-1), the bars list ended with the
    # MOST RECENT prevDailyBar (= yesterday's close), making
    # `daily_bars[-1]` yesterday's bar instead of today's. That inverted
    # today_pct, cum_3d, had_recent_5pct, etc. across the entire universe
    # — every CALL/PUT direction call was off by one day.
    #
    # Fix: dedupe on bar's own `t` timestamp (the date string, first 10
    # chars). prevDailyBar is only added if its date isn't already in the
    # set (gives us 1 extra prior day for the OLDEST snapshot).
    def get_daily_history(tkr):
        by_date = {}  # 'YYYY-MM-DD' → bar dict
        for snap_name, snap in snaps_loaded:
            tdata = snap.get('data', {}).get(tkr, {})
            ap = tdata.get('alpaca_snapshot') or {}
            db, pdb = ap.get('dailyBar') or {}, ap.get('prevDailyBar') or {}
            for bar in (db, pdb):
                c = bar.get('c'); t = bar.get('t')
                if c is None or not t: continue
                iso = t[:10]  # 'YYYY-MM-DD'
                if iso in by_date: continue  # already have this date — skip dup
                by_date[iso] = {'t': iso, 'o': bar.get('o'), 'h': bar.get('h'),
                                'l': bar.get('l'), 'c': bar.get('c'),
                                'v': bar.get('v', 0)}
        return [by_date[k] for k in sorted(by_date.keys())]

    # Load enrichment signals (graceful: empty dict if file missing)
    uw_signals_per_ticker = load_uw_signals()

    # Use WS live data overlay for TODAY's flow/DP instead of the snapshot.
    # Snapshot still provides static fields (info, iv_rank) and yest/2-day-ago
    # data (multi-day comparisons), serves as fallback when WS data unavailable.
    try:
        from v4_ws_live_data import build_live_blob
        USE_LIVE = True
        print('[live overlay] WS live data enabled', file=sys.stderr)
    except Exception as e:
        USE_LIVE = False
        print(f'[live overlay] WS unavailable, using snapshot only: {e}', file=sys.stderr)

    results = []
    for tkr, blob in today_data.items():
        bars = get_daily_history(tkr)
        if not bars or len(bars) < 3: continue
        # Live blob overrides today's flow/DP/net_prem_ticks; static + multi-day from snapshot.
        live_blob = build_live_blob(tkr, snapshot_blob=blob) if USE_LIVE else blob
        f = extract_features(tkr, live_blob,
                              yest_data.get(tkr), twoago_data.get(tkr), bars)
        if not f: continue
        # Active scorer (v8 by default, see SCORE_FN_VERSION). Also compute v7
        # in parallel so v4_formula_weekly_compare.py can track v7 vs v8 hit-rate
        # divergence as data grows. v7_score / v7_put_score are NOT thresholded
        # — they're observational only.
        uw = uw_signals_per_ticker.get(tkr)
        score        = _SCORE_FN(f, uw_signals=uw)
        put_score    = _SCORE_FN_PUT(f, uw_signals=uw)
        v7_score     = staging_score_v7(f, uw_signals=uw)
        v7_put_score = staging_score_v7_put(f, uw_signals=uw)
        # Directional gate: stocks clearly down can't be CALL setups, and vice
        # versa. Without this, structural-bonus dual-fire is rampant (e.g.
        # INTC -3.35% today scoring 224 CALL + 212 PUT — clearly bearish, but
        # both crossed threshold from beta+sector+mcap+UW alone).
        score_pre_gate, put_score_pre_gate = score, put_score
        score, put_score = _apply_directional_gate(f, score, put_score)
        if score == 0 and put_score == 0: continue  # filtered by hard gates on both sides
        pattern, why = classify_pattern({**f, 'score': score})
        put_pattern, put_why = classify_pattern_put({**f, 'score': put_score}) if put_score > 0 else (None, None)
        results.append({**f,
                        'score': score,
                        'put_score': put_score,
                        'score_pre_gate': score_pre_gate,
                        'put_score_pre_gate': put_score_pre_gate,
                        'v7_score': v7_score,
                        'v7_put_score': v7_put_score,
                        'score_version': SCORE_FN_VERSION,
                        'signals': signal_breakdown(f),
                        'pattern': pattern,
                        'pattern_why': why,
                        'put_pattern': put_pattern,
                        'put_pattern_why': put_why,
                        'is_buy': score >= BUY_THRESHOLD,
                        'is_put_buy': put_score >= PUT_BUY_THRESHOLD,
                        'direction': ('BULLISH' if score >= max(put_score, BUY_THRESHOLD)
                                       else 'BEARISH' if put_score >= PUT_BUY_THRESHOLD else None)})

    results.sort(key=lambda r: -max(r['score'], r['put_score']))
    buys = [r for r in results if r['is_buy']]
    put_buys = [r for r in results if r['is_put_buy']]

    payload = {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot_used': today_name,
        'universe_size': len(today_data),
        'scored_count': len(results),
        'buy_threshold': BUY_THRESHOLD,
        'put_buy_threshold': PUT_BUY_THRESHOLD,
        'buy_count': len(buys),
        'put_buy_count': len(put_buys),
        'methodology': f'{SCORE_FN_VERSION} — winner-distribution-weighted bidirectional scoring. v7 score also computed for compare. PUT side experimental.',
        'score_version': SCORE_FN_VERSION,
        'expected_hit_rate': '(v8 awaiting first-week validation; v7 baseline 40% top-20 in 04-20→24 sample, 20% top-10 in 04-22→29 sample). PUT side awaiting validation.',
        'buys': buys,
        'put_buys': put_buys,
        'top_50': results[:50],
        'all_scored': results,
    }

    with open(OUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"💾 saved {OUT_PATH} ({len(results)} scored, top score {results[0]['score'] if results else 0})", file=sys.stderr)

    # Print top 15 to stdout
    print(f"\n{'='*100}")
    print(f"V4 STAGING — top 15 picks for next-day positioning")
    print(f"{'='*100}")
    print(f"{'#':<3} {'ticker':<7} {'score':<6} {'sector':<22} {'cum_3d':<8} {'signals'}")
    print('-'*120)
    for i, r in enumerate(results[:15], 1):
        sigs = ' · '.join(r['signals'][:5])
        print(f"{i:<3} {r['ticker']:<7} {r['score']:<6} {r['sector'][:22]:<22} {r['cum_3d']:>+5.1f}%   {sigs}")


if __name__ == '__main__':
    main()
