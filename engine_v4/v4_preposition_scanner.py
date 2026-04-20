"""
V4 PRE-POSITION SCANNER — multi-day buildup detector.

Runs once daily, ideally after market close (1:35 PM PT). Scans a Tier-1 list
of mega-caps for tickers showing the early structural signs of a coordinated
breakout that the intraday V4 engine can't see in a single session.

DESIGN PRINCIPLE: standalone, advisory, read-only on the live system.
Writes to v4_preposition_watchlist.json. The dashboard /breakouts page reads
it. V4 daemon does NOT consume this output (no integration tonight — that's
a deliberate later step once we've validated the scanner's calls against
real outcomes).

SIGNALS SCORED (max 100 points):
  1. Compression          → 0-20  (10-day daily range tightening)
  2. Persistent flow      → 0-25  (5-day net call/put ASK premium bias)
  3. Multi-day dark pool  → 0-15  (5-day cluster validation score)
  4. Sector relative str. → 0-15  (5-day outperformance vs SPY)
  5. GEX flip distance    → 0-15  (STUB — v2)
  6. Skew flatten         → 0-10  (STUB — v2)

Threshold for inclusion in output: score ≥ 40

API: 11 UW calls per ticker × 15 tickers = 165 UW calls per run. Trivial.
Run on demand: python3 engine_v4/v4_preposition_scanner.py
"""
import os
import sys
import json
import math
import time
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

# UW endpoint helpers (Phase 2b/4a/4c): squeeze, seasonality, sector ETF batch
from v4_uw_helpers import squeeze_score, seasonality_score, sector_etf_strength

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

UTC = timezone.utc
UW_API_KEY = os.getenv("UW_API_KEY", "").replace('"', '')
HEADERS = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
BASE_URL = "https://api.unusualwhales.com"

OUT_PATH = os.path.join(os.path.dirname(__file__), 'v4_preposition_watchlist.json')

# Tier-1 always-watched universe. These 15 are the mega-caps that anchor the scan —
# they drive the indices and should always be evaluated regardless of daily flow.
# Additional names come dynamically from select_dynamic_universe() based on
# real options activity (top by OI / net premium). No more hardcoding.
TIER1_WATCHLIST = [
    'NVDA', 'MSFT', 'AAPL', 'GOOGL', 'META', 'AMZN', 'AVGO', 'TSLA',
    'AMD', 'NFLX', 'PLTR', 'COIN', 'MSTR', 'QQQ', 'SPY',
]

# Tier 3 = PUT-bias universe, pulled DYNAMICALLY from UW (no hardcoded tickers).
# Strategy: the top-net-impact endpoint returns tickers sorted by absolute net
# option premium; the ones with NEGATIVE net_premium (call-ask < put-ask) are
# names where institutions are net BUYING PUTS — that's exactly our PUT-scout
# universe. Per-day ranking updates automatically as flow shifts.
TIER3_FETCH_LIMIT = 150  # pull a deep slice; the negative-premium tail is what we want
TIER3_MAX_EVALUATED = 20  # cap after dedup

# Tier 2 = dynamic universe derived from UW's /top-net-impact ranking.
# Names change daily based on real options premium flow. No hardcoding.
TIER2_FETCH_LIMIT = 50       # how many UW ranks to pull
TIER2_MAX_EVALUATED = 35     # cap on how many we actually score (after dedup)


def fetch_tier2_universe():
    """Dynamic Tier 2 universe from UW's net-premium ranking (top tickers by flow today).
    Takes POSITIVE net_premium (call-heavy names). Dedupes against Tier 1."""
    j = _uw_get(f"{BASE_URL}/api/market/top-net-impact?limit={TIER2_FETCH_LIMIT}")
    if isinstance(j, dict): j = j.get('data', [])
    if not isinstance(j, list): return []
    tier1_set = set(TIER1_WATCHLIST)
    out = []
    for r in j:
        if not isinstance(r, dict): continue
        t = r.get('ticker', '').strip().upper()
        if not t or t in tier1_set: continue
        try:
            net_prem = float(r.get('net_premium', 0))
        except: net_prem = 0
        # Tier 2 = call-biased (positive net premium)
        if net_prem <= 0: continue
        out.append({'ticker': t, 'tier2_net_premium': net_prem})
        if len(out) >= TIER2_MAX_EVALUATED: break
    return out


def fetch_tier3_universe(exclude_tickers=None):
    """Dynamic Tier 3 universe — PUT-biased names from same UW endpoint.
    Takes the NEGATIVE net_premium tail (put-heavy flow). Dedupes vs exclude_tickers.
    Returns [{'ticker':..., 'tier3_net_premium':...}]."""
    exclude = set(exclude_tickers or [])
    j = _uw_get(f"{BASE_URL}/api/market/top-net-impact?limit={TIER3_FETCH_LIMIT}")
    if isinstance(j, dict): j = j.get('data', [])
    if not isinstance(j, list): return []
    # Sort by most negative first (heaviest PUT flow)
    neg = []
    for r in j:
        if not isinstance(r, dict): continue
        t = r.get('ticker', '').strip().upper()
        if not t or t in exclude: continue
        try:
            net_prem = float(r.get('net_premium', 0))
        except: net_prem = 0
        if net_prem >= 0: continue  # only put-heavy
        neg.append({'ticker': t, 'tier3_net_premium': net_prem})
    neg.sort(key=lambda x: x['tier3_net_premium'])  # most negative first
    return neg[:TIER3_MAX_EVALUATED]

LOOKBACK_DAYS = 5
COMPRESSION_DAYS = 10
SCORE_THRESHOLD = 40  # Reverted from 35 → 40 (2026-04-17 17:10): empirical check on the
                      # 33 rejections in 30-39 band showed 12% +1% wins vs 21% -1% losses,
                      # avg -0.34% vs SPY. Lowering would have added more losers than winners.
                      # The 40 threshold IS doing real filtering work — keep it.

# Rate limiter (UW only)
_uw_calls = 0
_last_uw = 0.0
def _uw_pace(min_gap=0.8):
    global _uw_calls, _last_uw
    elapsed = time.time() - _last_uw
    if elapsed < min_gap: time.sleep(min_gap - elapsed)
    _last_uw = time.time()
    _uw_calls += 1

def _uw_get(url, retries=2):
    backoff = 2.0
    for attempt in range(retries):
        _uw_pace()
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = backoff * (attempt + 1)
                print(f"    ⏳ 429 — backing off {wait:.0f}s")
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            print(f"    ⚠️ {e}")
            return None
    return None


def trading_days_back(end_date, n):
    days = []
    d = end_date
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


# ============ SIGNAL 1: COMPRESSION ============
def score_compression(ticker, ref_date):
    """0-20 points based on tightness of 10-day daily range."""
    df = fetch_alpaca_bars(
        ticker, '1Day',
        (ref_date - timedelta(days=COMPRESSION_DAYS + 5)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    if df is None or df.empty or len(df) < COMPRESSION_DAYS:
        return {"score": 0, "days": 0, "range_pct": None}
    recent = df.tail(COMPRESSION_DAYS)
    rng = float(recent['High'].max() - recent['Low'].min())
    median_close = float(recent['Close'].median())
    range_pct = rng / median_close if median_close > 0 else 1.0

    if range_pct < 0.03:   score = 20
    elif range_pct < 0.05: score = 12
    elif range_pct < 0.07: score = 5
    else:                  score = 0

    return {"score": score, "days": COMPRESSION_DAYS,
            "range_pct": round(range_pct, 4),
            "high": round(float(recent['High'].max()), 2),
            "low": round(float(recent['Low'].min()), 2)}


# ============ SIGNAL 2: PERSISTENT FLOW ============
def score_persistent_flow(ticker, ref_date, force_direction=None):
    """0-25 points + direction. Net (call_ask - put_ask) premium per day over LOOKBACK_DAYS.

    force_direction='CALL' → score the call-side even if net is negative
                  ='PUT'  → score the put-side even if net is positive
                  None    → auto-pick dominant direction (legacy behavior)

    Used for dual-direction scanning on Tier 1 mega-caps: we evaluate both sides
    so a mega-cap with persistent CALL flow AND mild PUT interest can produce
    two candidates if both pass the 200 SMA regime gate (rare but possible —
    e.g., protection bids into earnings).
    """
    days = trading_days_back(ref_date, LOOKBACK_DAYS)
    daily_net = []
    for d in days:
        date_str = d.strftime('%Y-%m-%d')
        j = _uw_get(f"{BASE_URL}/api/stock/{ticker}/flow-per-strike?date={date_str}")
        if not isinstance(j, list):
            continue
        call_ask = sum(float(r.get('call_premium_ask_side') or 0) for r in j)
        put_ask  = sum(float(r.get('put_premium_ask_side') or 0) for r in j)
        daily_net.append((date_str, call_ask - put_ask))

    if not daily_net:
        return {"score": 0, "direction": force_direction or "CALL", "days_with_data": 0,
                "net_call_premium_avg": 0}

    bullish_days = sum(1 for _, n in daily_net if n > 5_000_000)
    moderate_bull = sum(1 for _, n in daily_net if n > 2_000_000)
    light_bull = sum(1 for _, n in daily_net if n > 1_000_000)

    bearish_days = sum(1 for _, n in daily_net if n < -5_000_000)
    moderate_bear = sum(1 for _, n in daily_net if n < -2_000_000)
    light_bear = sum(1 for _, n in daily_net if n < -1_000_000)

    avg_net = sum(n for _, n in daily_net) / len(daily_net)
    auto_direction = "CALL" if avg_net >= 0 else "PUT"
    direction = force_direction if force_direction in ('CALL', 'PUT') else auto_direction

    if direction == "CALL":
        if bullish_days >= 4: score = 25
        elif moderate_bull >= 3: score = 15
        elif light_bull >= 3: score = 8
        else: score = 0
    else:
        if bearish_days >= 4: score = 25
        elif moderate_bear >= 3: score = 15
        elif light_bear >= 3: score = 8
        else: score = 0

    return {
        "score": score, "direction": direction,
        "days_with_data": len(daily_net),
        "net_call_premium_avg": round(avg_net, 0),
        "daily": [{"date": d, "net": round(n, 0)} for d, n in daily_net],
    }


# ============ SIGNAL 3: MULTI-DAY DARK POOL ============
def score_darkpool(ticker, ref_date):
    """0-15 points using validation_score (0-4 sub-scoring per the desk-review feedback).
    +1: prints across ≥3 days
    +1: cumulative size ≥5% of avg daily volume
    +1: spot price holding above the cluster price level"""
    days = trading_days_back(ref_date, LOOKBACK_DAYS)
    all_prints = []  # (date, price, size)
    for d in days:
        date_str = d.strftime('%Y-%m-%d')
        j = _uw_get(f"{BASE_URL}/api/darkpool/{ticker}?date={date_str}")
        if not isinstance(j, dict):
            continue
        for p in j.get('data', []) or []:
            try:
                pr = float(p.get('price', 0))
                sz = int(p.get('size', 0))
                if pr > 0 and sz > 0:
                    all_prints.append((date_str, pr, sz))
            except: continue

    if not all_prints:
        return {"score": 0, "validation_score": 0, "cluster_price": None,
                "total_size": 0, "days_with_prints": 0}

    # Cluster prints by price level (±0.5%)
    sorted_prints = sorted(all_prints, key=lambda x: x[1])
    clusters = []  # each = list of (date, price, size)
    current = [sorted_prints[0]]
    for p in sorted_prints[1:]:
        ref_price = current[0][1]
        if abs(p[1] - ref_price) / ref_price <= 0.005:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)

    # Pick the largest cluster by total size
    best = max(clusters, key=lambda c: sum(p[2] for p in c))
    cluster_price = sum(p[1] * p[2] for p in best) / sum(p[2] for p in best)
    cluster_total_size = sum(p[2] for p in best)
    cluster_dates = set(p[0] for p in best)

    # Get average daily volume from Alpaca
    df_vol = fetch_alpaca_bars(
        ticker, '1Day',
        (ref_date - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    adv = float(df_vol['Volume'].mean()) if df_vol is not None and not df_vol.empty else 1
    size_pct_of_adv = (cluster_total_size / adv) if adv > 0 else 0

    # Get current spot
    spot = float(df_vol['Close'].iloc[-1]) if df_vol is not None and not df_vol.empty else cluster_price

    validation_score = 0
    if len(cluster_dates) >= 3: validation_score += 1
    if size_pct_of_adv >= 0.05: validation_score += 1
    if spot >= cluster_price * 0.99: validation_score += 1
    # 4th criterion (intraday support test) deferred — needs intraday bars

    if validation_score >= 3:   score = 15
    elif validation_score == 2: score = 10
    elif validation_score == 1: score = 5
    else:                       score = 0

    return {
        "score": score, "validation_score": validation_score,
        "cluster_price": round(cluster_price, 2),
        "total_size": cluster_total_size,
        "size_pct_of_adv": round(size_pct_of_adv, 3),
        "days_with_prints": len(cluster_dates),
        "spot_held_above": spot >= cluster_price * 0.99,
    }


# ============ SIGNAL 4b: 200 SMA TREND REGIME ============
# Not a lit signal (doesn't take pts), acts as a hard gate + context flag.
# Humbled Trader: "only long names above 200 SMA, only short names below."
def score_trend_regime(ticker, direction, ref_date, flow_score=0):
    """Gate: spot vs 200-day SMA, with asymmetric logic per direction.

    CALL: requires spot > 200 SMA — don't buy calls into a downtrend.
    PUT: passes if ANY of:
         (a) spot < 200 SMA — classic weakness, or
         (b) spot > 200 SMA × 1.15 — overextended, pullback-ripe, or
         (c) persistent PUT flow_score >= 15 — institutional hedging signal
             strong enough to override the regime filter (earnings protection,
             sector rotation out of a strong name, failed breakout setups).

    Rationale: the original symmetric "PUT needs spot < 200 SMA" rule was
    backwards from actual trading. Best PUT setups live in STRONG names
    rolling over (overextension + put-flow), not already-broken names.
    """
    try:
        df = fetch_alpaca_bars(
            ticker, '1Day',
            (ref_date - timedelta(days=400)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
        )
        if df is None or df.empty or len(df) < 200:
            return {"pass": True, "note": "insufficient history — permissive"}
        sma_200 = float(df['Close'].tail(200).mean())
        spot = float(df['Close'].iloc[-1])
        pct_vs_sma = (spot - sma_200) / sma_200 * 100
        if direction == 'CALL':
            ok = spot > sma_200
            gate_reason = 'spot > 200 SMA' if ok else f'below SMA ({pct_vs_sma:+.1f}%)'
        else:
            # PUT: three independent pass conditions
            below_sma = spot < sma_200
            overextended = pct_vs_sma > 15.0
            strong_put_flow = flow_score >= 15
            ok = below_sma or overextended or strong_put_flow
            if below_sma:        gate_reason = f'spot < 200 SMA ({pct_vs_sma:+.1f}%)'
            elif overextended:   gate_reason = f'overextended ({pct_vs_sma:+.1f}% above SMA)'
            elif strong_put_flow: gate_reason = f'strong put flow overrides regime'
            else:                 gate_reason = f'SMA fail, not overextended, weak flow'
        return {
            "pass": ok,
            "sma_200": round(sma_200, 2), "spot": round(spot, 2),
            "pct_vs_sma": round(pct_vs_sma, 2),
            "regime": "BULL" if spot > sma_200 else "BEAR",
            "gate_reason": gate_reason,
        }
    except Exception as e:
        return {"pass": True, "note": f"error: {str(e)[:60]}"}


# ============ SIGNAL 4: SECTOR RELATIVE STRENGTH ============
def score_sector_strength(ticker, ref_date, spy_5d_return=None):
    """0-15 points based on outperformance vs SPY over 5 days."""
    df = fetch_alpaca_bars(
        ticker, '1Day',
        (ref_date - timedelta(days=LOOKBACK_DAYS + 3)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    if df is None or df.empty or len(df) < 2:
        return {"score": 0, "ticker_return_pct": 0, "spy_return_pct": spy_5d_return, "outperformance_pct": 0}
    base_close = float(df['Close'].iloc[0])
    if base_close <= 0:  # guard against zero/negative open price (penny-stock edge case)
        return {"score": 0, "ticker_return_pct": 0, "spy_return_pct": spy_5d_return, "outperformance_pct": 0}
    ticker_ret = (float(df['Close'].iloc[-1]) - base_close) / base_close

    if spy_5d_return is None:
        spy_5d_return = 0
    outperf = ticker_ret - spy_5d_return

    if outperf > 0.03:   score = 15
    elif outperf > 0.015: score = 10
    elif outperf > 0.005: score = 5
    else:                 score = 0

    return {
        "score": score,
        "ticker_return_pct": round(ticker_ret * 100, 2),
        "spy_return_pct": round(spy_5d_return * 100, 2),
        "outperformance_pct": round(outperf * 100, 2),
    }


# ============ SIGNAL 5: GEX REGIME (formerly "GEX Flip") ============
# UW's /greek-exposure endpoint returns aggregate net gamma per day (250-day history).
# It does NOT give per-strike OI-weighted GEX, so we can't compute the literal flip
# strike. Instead we score the GAMMA REGIME: is today's dealer gamma exposure in the
# "short" regime (amplify — breakout-friendly) or "long" regime (suppress)?
# Percentile-based so it normalizes across ticker sizes.
def score_gex_flip(ticker, ref_date):
    """0-15 points based on where today's net dealer gamma sits in its 1-year distribution.
    Lower percentile = more short gamma = amplification regime = good for breakouts."""
    j = _uw_get(f"{BASE_URL}/api/stock/{ticker}/greek-exposure")
    # UW sometimes wraps as {"data": [...]} — normalize
    if isinstance(j, dict):
        j = j.get('data', [])
    if not isinstance(j, list) or not j:
        return {"score": 0, "regime": "UNKNOWN", "net_gamma": None, "percentile": None,
                "note": "no data from /greek-exposure"}

    # Parse and sort by date desc
    parsed = []
    for r in j:
        try:
            cg = float(r.get('call_gamma', 0))
            pg = float(r.get('put_gamma', 0))
            parsed.append({'date': r.get('date',''), 'net': cg + pg})
        except: continue
    if not parsed:
        return {"score": 0, "regime": "UNKNOWN", "net_gamma": None, "percentile": None}
    parsed.sort(key=lambda x: x['date'], reverse=True)

    today_net = parsed[0]['net']
    history = [x['net'] for x in parsed]
    # Percentile of today's value within the full distribution (lower = more short)
    below = sum(1 for v in history if v < today_net)
    percentile = (below / len(history)) * 100 if history else 50.0

    # Scoring: short gamma (low percentile) is breakout-friendly
    if today_net < 0:
        # Already short gamma — clear amplification regime
        score = 15
        regime = "SHORT_GAMMA"
    elif percentile <= 20:
        # Positive but near 1-year low → heading toward flip, squeeze potential
        score = 12
        regime = "APPROACHING_FLIP"
    elif percentile <= 40:
        score = 7
        regime = "MILD_LONG"
    elif percentile <= 70:
        score = 3
        regime = "LONG_GAMMA"
    else:
        # Top of range — maximum suppression, unfavorable for breakouts
        score = 0
        regime = "HEAVY_LONG"

    return {
        "score": score, "regime": regime,
        "net_gamma": round(today_net, 0),
        "percentile": round(percentile, 1),
        "history_n": len(history),
        "distance_pct": None,  # leave for future per-strike version
    }


# ============ SIGNAL 6: SKEW FLAT ============
# UW's /greeks?expiry=X returns per-strike deltas + IV. We locate the 25-delta put and
# 25-delta call strikes, compare their IVs — that's the standard skew measurement.
# "Flat skew" = low spread between put/call IV = market not paying for downside protection.
# Higher score for flatter skew (confirmation signal, weight low per the critique).
def score_skew(ticker, ref_date):
    """0-10 points. Flat skew (low put/call IV premium) = favorable risk-on environment."""
    # Pick a target expiry ~30 DTE out. Find the first available expiry.
    # We query without expiry filter first; if UW returns a single expiry, we iterate.
    j = _uw_get(f"{BASE_URL}/api/stock/{ticker}/greeks")
    if isinstance(j, dict): j = j.get('data', [])
    if not isinstance(j, list) or not j:
        return {"score": 0, "note": "no greeks data"}

    # Group by expiry → find the one closest to 30 DTE from ref_date
    from collections import defaultdict
    by_exp = defaultdict(list)
    for r in j:
        exp = r.get('expiry')
        if exp: by_exp[exp].append(r)
    if not by_exp:
        return {"score": 0, "note": "no expiries in greeks"}

    target_exp_date = ref_date + timedelta(days=30)
    def exp_dist(exp_str):
        try: return abs((datetime.strptime(exp_str, '%Y-%m-%d') - target_exp_date).days)
        except: return 9999
    chosen_exp = min(by_exp.keys(), key=exp_dist)
    records = by_exp[chosen_exp]

    # If default endpoint returned only near-term, explicitly fetch the target expiry
    if exp_dist(chosen_exp) > 14:
        # Try to find a better expiry via explicit query
        target_exp_str = target_exp_date.strftime('%Y-%m-%d')
        j2 = _uw_get(f"{BASE_URL}/api/stock/{ticker}/greeks?expiry={target_exp_str}")
        if isinstance(j2, dict): j2 = j2.get('data', [])
        if isinstance(j2, list) and j2:
            records = j2
            chosen_exp = j2[0].get('expiry', chosen_exp)

    # Find closest strike to 25-delta call and 25-delta put
    def closest_to(target_delta, records, side):
        best = None; best_dist = 999
        for r in records:
            try:
                d = float(r.get(f'{side}_delta', 0))
                if side == 'put': d = abs(d)  # put deltas are negative
                dist = abs(d - target_delta)
                if dist < best_dist:
                    best_dist = dist; best = r
            except: continue
        return best

    call_25d = closest_to(0.25, records, 'call')
    put_25d = closest_to(0.25, records, 'put')
    atm = closest_to(0.50, records, 'call')  # reference for normalization

    if not (call_25d and put_25d and atm):
        return {"score": 0, "note": "couldn't locate 25-delta strikes"}

    try:
        call_iv = float(call_25d.get('call_volatility', 0))
        put_iv = float(put_25d.get('put_volatility', 0))
        atm_iv = float(atm.get('call_volatility', 0))
    except:
        return {"score": 0, "note": "IV parse failed"}

    if atm_iv <= 0:
        return {"score": 0, "note": "invalid ATM IV"}

    # Raw skew = put_iv - call_iv (positive = puts more expensive)
    # Normalized by ATM IV so it's comparable across tickers
    skew_absolute = put_iv - call_iv
    skew_pct = (skew_absolute / atm_iv) * 100 if atm_iv > 0 else 0

    # Scoring: flatter (lower absolute skew) = more points
    abs_skew = abs(skew_pct)
    if abs_skew < 3:   score = 10
    elif abs_skew < 6: score = 7
    elif abs_skew < 10: score = 4
    elif abs_skew < 15: score = 1
    else: score = 0

    return {
        "score": score,
        "skew_pct": round(skew_pct, 2),
        "put_iv_25d": round(put_iv, 4),
        "call_iv_25d": round(call_iv, 4),
        "atm_iv": round(atm_iv, 4),
        "expiry_used": chosen_exp,
        "put_strike": put_25d.get('strike'),
        "call_strike": call_25d.get('strike'),
    }


# ============ MAIN ============
def get_spy_5d_return(ref_date):
    try:
        df = fetch_alpaca_bars(
            'SPY', '1Day',
            (ref_date - timedelta(days=LOOKBACK_DAYS + 3)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
        )
        if df is None or df.empty or len(df) < 2: return 0
        return (float(df['Close'].iloc[-1]) - float(df['Close'].iloc[0])) / float(df['Close'].iloc[0])
    except Exception as e:
        print(f"⚠️ SPY fetch failed, using 0 baseline: {e}")
        return 0


def build_narrative(t, comp, flow, dp, sector):
    parts = []
    if comp['score'] > 0:
        parts.append(f"{COMPRESSION_DAYS}d compression {comp['range_pct']*100:.1f}%")
    if flow['score'] > 0:
        days = flow.get('days_with_data', 0)
        avg_m = flow['net_call_premium_avg'] / 1e6
        parts.append(f"persistent {flow['direction']} bias ${avg_m:+.1f}M/day×{days}d")
    if dp['score'] > 0:
        parts.append(f"DP cluster @ {dp['cluster_price']} (val {dp['validation_score']}/4)")
    if sector['score'] > 0:
        parts.append(f"+{sector['outperformance_pct']:+.1f}% vs SPY")
    return f"{t}: " + " | ".join(parts) if parts else f"{t}: no signals"


def scan():
    ref_date = datetime.utcnow().date()
    while ref_date.weekday() >= 5:
        ref_date -= timedelta(days=1)
    print(f"\n{'='*70}")
    print(f"V4 PRE-POSITION SCANNER | ref date: {ref_date}")
    print(f"Tier-1 mega cap: {len(TIER1_WATCHLIST)} tickers (hardcoded anchors)")
    tier2_records = fetch_tier2_universe()
    tier2_tickers = [r['ticker'] for r in tier2_records]
    print(f"Tier-2 dynamic:  {len(tier2_tickers)} tickers from /top-net-impact (flow-ranked)")
    print(f"Threshold: score ≥ {SCORE_THRESHOLD}")
    print(f"{'='*70}\n")

    spy_5d = get_spy_5d_return(datetime.combine(ref_date, datetime.min.time()))
    print(f"SPY 5d return: {spy_5d*100:+.2f}%\n")

    # Phase 4c: Snapshot sector ETFs (one UW call, all 11 sectors). Used as
    # macro context for the dashboard and future sector-RS refinements.
    try:
        sector_snapshot = sector_etf_strength()
        leaders = sorted(sector_snapshot.items(), key=lambda kv: -kv[1].get('pct_change', 0))[:3]
        laggards = sorted(sector_snapshot.items(), key=lambda kv: kv[1].get('pct_change', 0))[:3]
        if leaders:
            lead_str = ', '.join(f"{t} {d['pct_change']:+.2f}%" for t, d in leaders)
            lag_str = ', '.join(f"{t} {d['pct_change']:+.2f}%" for t, d in laggards)
            print(f"Sector leaders today: {lead_str}")
            print(f"Sector laggards today: {lag_str}\n")
    except Exception as e:
        sector_snapshot = {}
        print(f"sector ETF snapshot unavailable: {type(e).__name__}\n")

    # Build combined scan universe, tagged by source + direction.
    # Tier 1 mega-caps are scanned in BOTH directions so a META above its 200 SMA
    # can still produce a PUT candidate if institutions are buying protection and
    # the setup flips to bearish. Tier 2/3 use auto-picked direction (single pass).
    # Tuple: (ticker, tier, net_premium, force_direction)
    universe = []
    for t in TIER1_WATCHLIST:
        universe.append((t, 1, None, 'CALL'))
        universe.append((t, 1, None, 'PUT'))
    universe += [(r['ticker'], 2, r.get('tier2_net_premium'), None) for r in tier2_records]
    tier1_set = set(TIER1_WATCHLIST)
    t2_set = {r['ticker'] for r in tier2_records}

    # Tier 3 — dynamically pulled PUT-bias universe (negative net-premium tail)
    try:
        tier3_records = fetch_tier3_universe(exclude_tickers=list(tier1_set) + list(t2_set))
    except Exception as e:
        print(f"  tier 3 fetch failed: {e} — continuing without PUT universe")
        tier3_records = []
    universe += [(r['ticker'], 3, r.get('tier3_net_premium'), None) for r in tier3_records]
    print(f"Tier 3 (dynamic PUT-bias): {len(tier3_records)} tickers — sample: "
          f"{[r['ticker'] for r in tier3_records[:5]]}")

    candidates = []
    for t, tier, t2_prem, force_dir in universe:
        label = f"{t}-{force_dir}" if force_dir else t
        print(f"— {label}")
        ref_dt = datetime.combine(ref_date, datetime.min.time())
        try:
            comp = score_compression(t, ref_dt)
            flow = score_persistent_flow(t, ref_dt, force_direction=force_dir)
            dp = score_darkpool(t, ref_dt)
            sector = score_sector_strength(t, ref_dt, spy_5d)
            gex = score_gex_flip(t, ref_dt)
            skew = score_skew(t, ref_dt)
            trend = score_trend_regime(t, flow.get('direction', 'CALL'), ref_dt,
                                       flow_score=flow.get('score', 0))
        except Exception as e:
            print(f"  ⚠️ scoring failed for {t}: {type(e).__name__}: {str(e)[:100]} — skipping")
            continue

        # 200 SMA gate: CALL must be above, PUT must be below. Reject before scoring.
        if not trend.get('pass', True):
            print(f"  🚫 {t:>5} | skip {flow.get('direction','?')} — spot ${trend.get('spot')} vs 200 SMA ${trend.get('sma_200')} ({trend.get('regime')})")
            continue

        direction = flow['direction']

        # Phase 4a: Seasonality — DISABLED after backtest (2026-04-18).
        # Gave every April signal a uniform -5 shift (April is historically weak across
        # most tech names), so it wasn't discriminating between good/bad trades — just
        # lowering every score equally. Re-enable when we have year-round data.
        seas_pts, seas_msg = 0, "seasonality disabled"

        # Phase 2b: Squeeze score — adds points for CALL setups on heavily-shorted names
        try:
            sq_pts, sq_msg = squeeze_score(t, direction)
        except Exception as e:
            sq_pts, sq_msg = 0, f"squeeze err: {type(e).__name__}"

        total = (comp['score'] + flow['score'] + dp['score'] + sector['score']
                 + gex['score'] + skew['score'] + seas_pts + sq_pts)

        narrative = build_narrative(t, comp, flow, dp, sector)
        if seas_pts:    narrative += f" | seas {seas_pts:+d} ({seas_msg})"
        if sq_pts:      narrative += f" | {sq_msg} ({sq_pts:+d})"
        tag = "🟢" if total >= SCORE_THRESHOLD else "·"
        print(f"  {tag} score={total:>3} | comp={comp['score']:>2} flow={flow['score']:>2} dp={dp['score']:>2} sector={sector['score']:>2} seas={seas_pts:+d} sq={sq_pts:+d} | {narrative}")

        if total >= SCORE_THRESHOLD:
            candidates.append({
                "ticker": t, "score": total, "direction": direction,
                "tier": tier,  # 1 = mega cap (anchor), 2 = dynamic (flow-ranked)
                "universe": "mega_cap" if tier == 1 else "dynamic",
                "tier2_rank_net_premium": t2_prem,  # None for tier 1
                "suggested_dte_min": 14, "suggested_dte_max": 30,
                "days_in_setup": comp.get('days', 0),
                "signals": {
                    "compression": comp,
                    "persistent_flow": flow,
                    "darkpool_accumulation": dp,
                    "sector_strength": sector,
                    "gex_regime": gex,
                    "gamma_flip_distance_pct": gex.get('distance_pct'),
                    "skew_flat": skew,
                    "trend_regime_200sma": trend,  # hard-gate result + context
                    "seasonality": {"score": seas_pts, "msg": seas_msg},
                    "squeeze": {"score": sq_pts, "msg": sq_msg},
                },
                "narrative": narrative,
                "key_levels": {
                    "compression_high": comp.get('high'),
                    "compression_low": comp.get('low'),
                    # Use the tighter of (compression_low - 1%) and (trigger - 1.5%).
                    # For compressed ranges the floor wins; for expanded ranges (mega-caps
                    # in trend) the tighter 1.5%-below-breakout wins, keeping R:R sane.
                    "stop_below": round(max(
                        comp['low'] * 0.99,
                        comp['high'] * 0.985
                    ), 2) if comp.get('low') and comp.get('high') else None,
                    "trigger_above": round(comp['high'] * 1.005, 2) if comp.get('high') else None,
                },
            })

    candidates.sort(key=lambda x: -x['score'])

    # ===== STRATEGY B1: Accumulation Scout =====
    # For every candidate score >= 40 AND price is still IN the compression range
    # (not gapped above trigger), emit an ACCUMULATION entry signal that bypasses
    # the breakout wait. Size will be 25% of normal — aggressive entry, small risk.
    accumulation_signals = []
    now_utc = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    for c in candidates:
        if c.get('score', 0) < 40: continue
        comp = (c.get('signals') or {}).get('compression') or {}
        comp_low = comp.get('low')
        comp_high = comp.get('high')
        if not (comp_low and comp_high): continue
        # Need live current price. Use the spot from trend_regime (last close).
        trend = (c.get('signals') or {}).get('trend_regime_200sma') or {}
        current_price = trend.get('spot')
        if not current_price: continue
        # Entry criteria: price still in compression range
        if current_price > comp_high * 1.005 or current_price < comp_low * 0.99:
            continue  # already broken out either direction — scout no longer applies

        direction = c.get('direction', 'CALL')
        # Direction-aware stop/target:
        # CALL setup: target is compression_HIGH (breakout up), stop is below comp_LOW (failure)
        # PUT  setup: target is compression_LOW (breakdown), stop is above comp_HIGH (failure)
        if direction == 'PUT':
            stop_level = round(comp_high * 1.01, 2)   # invalidation: moves UP out of range
            target_level = round(comp_low * 0.995, 2) # breakdown: moves DOWN below range
        else:
            stop_level = round(comp_low * 0.99, 2)    # invalidation: falls below range
            target_level = round(comp_high * 1.005, 2) # breakout: crosses range top

        # Build the accumulation signal
        accumulation_signals.append({
            'ticker': c.get('ticker'),
            'direction': direction,
            'score': c.get('score'),
            'tier': c.get('tier'),
            'entry_price_ref': current_price,
            'stop_below': stop_level,    # legacy field name — keeps dashboard compat
            'target_above': target_level, # legacy field name — dashboard interprets direction
            'compression_low': comp_low,
            'compression_high': comp_high,
            'size_mult': 0.25,
            'size_label': 'accumulation_scout_25pct',
            'generated_utc': now_utc,
            'narrative': c.get('narrative', ''),
            'suggested_dte_min': 14, 'suggested_dte_max': 30,
        })

    tier1_candidates = [c for c in candidates if c.get('tier') == 1]
    tier2_candidates = [c for c in candidates if c.get('tier') == 2]

    # Write accumulation signals to their own file
    accumulation_path = os.path.join(os.path.dirname(__file__), 'v4_accumulation_signals.json')
    accumulation_payload = {
        'metadata': {
            'generated_utc': now_utc,
            'total_signals': len(accumulation_signals),
            'size_default': 0.25,
            'strategy': 'B1_accumulation_scout',
            '_doc': 'Enter at current price BEFORE breakout, 25% size. Stop at compression_low-1%. Target at compression_high.',
        },
        'signals': accumulation_signals,
    }
    import tempfile as _tmp
    fd, tmp = _tmp.mkstemp(dir=os.path.dirname(accumulation_path) or '.', prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(accumulation_payload, f, indent=2, default=str)
        os.replace(tmp, accumulation_path)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
    print(f"🎯 Accumulation Scout: {len(accumulation_signals)} signals written → {accumulation_path}")
    payload = {
        "metadata": {
            "scan_date": str(ref_date),
            "scan_completed_utc": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            "tier1_scanned": len(TIER1_WATCHLIST),
            "tier1_passed": len(tier1_candidates),
            "tier2_scanned": len(tier2_tickers),
            "tier2_passed": len(tier2_candidates),
            "uw_calls_used": _uw_calls,
            "spy_5d_return_pct": round(spy_5d * 100, 2),
            "sector_etf_snapshot": sector_snapshot,
            "_schema_doc": "Pre-position scanner output. Each candidate tagged with tier: 1 (mega-cap anchor) or 2 (dynamic/flow-ranked). Dashboard splits by tier.",
        },
        "candidates": candidates,
    }

    # Before overwriting the watchlist, archive the PREVIOUS day's file if it
    # exists. Otherwise yesterday's triggered setups are lost forever when
    # today's scan runs. Archive goes to v4_preposition_archive/YYYY-MM-DD.json
    # keyed off the PREVIOUS scan's date (not today's) so the archive reflects
    # the day those triggers actually fired.
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH) as f: prev = json.load(f)
            prev_date = (prev.get('metadata', {}).get('scan_completed_utc') or '')[:10]
            if prev_date:
                archive_dir = os.path.join(os.path.dirname(__file__), 'v4_preposition_archive')
                os.makedirs(archive_dir, exist_ok=True)
                archive_path = os.path.join(archive_dir, f'{prev_date}.json')
                # Only archive if there's actually triggered content worth keeping
                triggered_count = sum(1 for c in prev.get('candidates', []) if c.get('triggered_at_utc'))
                if triggered_count > 0 or not os.path.exists(archive_path):
                    with open(archive_path, 'w') as f: json.dump(prev, f, indent=2, default=str)
                    print(f"📦 Archived previous watchlist → {archive_path} ({triggered_count} triggered)")
        except Exception as e:
            print(f"⚠️  Archive of previous watchlist failed: {e} — continuing with new write")

    # Atomic write — patrol writes to this same file; non-atomic risks partial-read corruption
    import tempfile
    dir_path = os.path.dirname(OUT_PATH) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, OUT_PATH)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
        raise

    print(f"\n{'='*70}")
    print(f"DONE | UW calls: {_uw_calls} | candidates: {len(candidates)}")
    print(f"{'='*70}")
    if candidates:
        print(f"\nTop candidates:")
        for c in candidates[:5]:
            print(f"  {c['ticker']:>5} {c['direction']:>4} score={c['score']:>3} → {c['narrative']}")
    print(f"\n💾 Output: {OUT_PATH}")


if __name__ == "__main__":
    scan()
