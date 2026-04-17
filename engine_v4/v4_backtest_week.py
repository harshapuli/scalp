"""
V4 BACKTEST — last N trading days (default 5).

Uses /api/stock/{ticker}/flow-per-strike?date=YYYY-MM-DD for historical option flow
(it's the only UW endpoint that supports true date filtering — flow-alerts ignores
all date params and returns the same recent ~24h regardless).

REDUCED-FIDELITY CAVEATS vs the live v4 engine:
- Greeks (gamma_per_one_percent_move_dir): snapshot-only, no historical → SKIPPED.
- IV rank: snapshot-only, no historical → SKIPPED.
- Dark pool confidence: snapshot-only → SKIPPED.
- Flow data is per-strike-per-timestamp aggregations, not raw alert clusters,
  so "sweep_count" is reframed as the number of distinct (strike, window) records
  that contributed; persistence semantics are preserved but not identical.
- Time-of-day, ask-dominance, log-scale premium, regime, SMC structure, watch
  retest, volume-confirmation breakouts: ALL preserved.

API budget per (day, ticker): 1 UW call. 5 days × 10 tickers = 50 UW calls
total, paced at ~3/s = under 200/min. Alpaca calls don't count vs UW limits.
"""
import os
import sys
import json
import math
import time
import requests
from datetime import datetime, timedelta, time as dtime, timezone
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars
from swing_trade_strategy.smc_engine import detect_fvg

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

UTC = timezone.utc
UW_API_KEY = os.getenv("UW_API_KEY", "").replace('"', '')
HEADERS = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
BASE_URL = "https://api.unusualwhales.com"

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', 'ranked_watchlist.json')
RESULTS_PATH = os.path.join(os.path.dirname(__file__), 'v4_backtest_week.json')

DAYS_BACK = 5  # trading days to backtest
SCORE_THRESHOLD = 40  # same as live engine
MIN_BUCKET_PREMIUM = 100_000  # match engine's qualifying premium gate

# ---------- Rate limiting (UW only — Alpaca runs free) ----------
# flow-per-strike triggers 429 well before 200/min — empirically capped lower.
# Use 0.8s spacing (~75/min) + exponential retry on 429.
_uw_calls = 0
_uw_429s = 0
_last_uw = 0.0
def _uw_pace(min_gap=0.8):
    global _uw_calls, _last_uw
    elapsed = time.time() - _last_uw
    if elapsed < min_gap: time.sleep(min_gap - elapsed)
    _last_uw = time.time()
    _uw_calls += 1


def _uw_get(url, timeout=15, max_retries=3):
    global _uw_429s
    backoff = 2.0
    for attempt in range(max_retries):
        _uw_pace()
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                _uw_429s += 1
                wait = backoff * (attempt + 1)
                print(f"    ⏳ 429 on {url[-50:]} — backing off {wait:.0f}s")
                time.sleep(wait)
                continue
            else:
                print(f"    ⚠️ UW {r.status_code}: {url[-60:]}")
                return None
        except Exception as e:
            print(f"    ⚠️ UW err: {e}")
            return None
    return None


# ---------- Trading-day helpers ----------
def trading_days(end_date, n):
    """Return list of last n trading days (Mon-Fri, US weekdays only) ending on or before end_date."""
    days = []
    d = end_date
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def rth_bounds(d):
    return (datetime.combine(d, dtime(13, 30), tzinfo=UTC),
            datetime.combine(d, dtime(20, 0), tzinfo=UTC))


# ---------- Data fetchers ----------
def fetch_flow_per_strike(ticker, date_str):
    """Returns list of records: each is a (strike, timestamp_window) aggregate."""
    j = _uw_get(f"{BASE_URL}/api/stock/{ticker}/flow-per-strike?date={date_str}")
    if not isinstance(j, list): return []
    out = []
    for r in j:
        ts = r.get('timestamp', '')
        try:
            t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            r['_ts'] = t
            out.append(r)
        except: continue
    out.sort(key=lambda x: x['_ts'])
    return out


def fetch_session_5min(ticker, rth_start, rth_end):
    return fetch_alpaca_bars(ticker, '5Min',
        rth_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        (rth_end + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'))


def fetch_25d_hourly(ticker, rth_end):
    return fetch_alpaca_bars(ticker, '1Hour',
        (rth_end - timedelta(days=25)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        rth_end.strftime('%Y-%m-%dT%H:%M:%SZ'))


def determine_regime_at(rth_end):
    df = fetch_alpaca_bars('SPY', '1D',
        (rth_end - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        rth_end.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty or len(df) <= 20: return "CHOP"
    sma20 = df['Close'].rolling(20).mean().iloc[-1]
    close = df['Close'].iloc[-1]
    if close > sma20 * 1.01: return "TREND_UP"
    if close < sma20 * 0.99: return "TREND_DOWN"
    return "CHOP"


# ---------- Bucketing & scoring ----------
def aggregate_buckets(records, rth_start, rth_end, bucket_minutes=15):
    """Group flow-per-strike records into directional buckets (CALL ASK / PUT ASK)
    over `bucket_minutes` windows. Returns list of bucket dicts ordered by time.
    Each bucket emits a separate CALL and PUT entry (so a strong PUT day shows up)."""
    buckets = defaultdict(lambda: {
        'call_ask_prem': 0.0, 'call_bid_prem': 0.0, 'call_volume': 0, 'call_trades': 0,
        'put_ask_prem': 0.0, 'put_bid_prem': 0.0, 'put_volume': 0, 'put_trades': 0,
        'first_ts': None, 'last_ts': None, 'records': 0, 'spot': 0.0,
    })

    def to_f(x):
        try: return float(x)
        except: return 0.0

    for r in records:
        ts = r['_ts']
        if not (rth_start <= ts <= rth_end): continue
        # Bucket by floor(min/bucket_minutes)
        bucket_min = (ts.minute // bucket_minutes) * bucket_minutes
        bkey = ts.replace(minute=bucket_min, second=0, microsecond=0)
        b = buckets[bkey]
        b['call_ask_prem'] += to_f(r.get('call_premium_ask_side'))
        b['call_bid_prem'] += to_f(r.get('call_premium_bid_side'))
        b['call_volume'] += int(r.get('call_volume') or 0)
        b['call_trades'] += int(r.get('call_trades') or 0)
        b['put_ask_prem'] += to_f(r.get('put_premium_ask_side'))
        b['put_bid_prem'] += to_f(r.get('put_premium_bid_side'))
        b['put_volume'] += int(r.get('put_volume') or 0)
        b['put_trades'] += int(r.get('put_trades') or 0)
        b['records'] += 1
        if b['first_ts'] is None or ts < b['first_ts']: b['first_ts'] = ts
        if b['last_ts'] is None or ts > b['last_ts']: b['last_ts'] = ts

    out = []
    for ts, b in sorted(buckets.items()):
        # Decide direction per bucket: whichever side has more ASK premium
        call_ask = b['call_ask_prem']; put_ask = b['put_ask_prem']
        if call_ask <= 0 and put_ask <= 0: continue
        # Emit one entry per direction that meets the minimum threshold
        if call_ask >= MIN_BUCKET_PREMIUM:
            ask_dom = call_ask / max(call_ask + b['call_bid_prem'], 1)
            out.append({
                'ts': ts, 'type': 'CALL', 'ask_prem': call_ask, 'bid_prem': b['call_bid_prem'],
                'volume': b['call_volume'], 'trades': b['call_trades'],
                'ask_dominance': ask_dom, 'records': b['records'],
            })
        if put_ask >= MIN_BUCKET_PREMIUM:
            ask_dom = put_ask / max(put_ask + b['put_bid_prem'], 1)
            out.append({
                'ts': ts, 'type': 'PUT', 'ask_prem': put_ask, 'bid_prem': b['put_bid_prem'],
                'volume': b['put_volume'], 'trades': b['put_trades'],
                'ask_dominance': ask_dom, 'records': b['records'],
            })
    return out


def score_bucket(bucket, regime):
    """Mirrors v4_meta_engine scoring minus the snapshot-only factors."""
    base = 0
    # Time-of-day
    est_hr = bucket['ts'].hour - 4 + (bucket['ts'].minute / 60.0)
    if 9.5 <= est_hr < 10.5: base += 5
    elif 11.5 <= est_hr <= 13.5: base -= 5

    # Log-scale premium
    prem_score = min(20, max(0, math.log10(max(bucket['ask_prem'] / 100_000, 1)) * 7))
    if bucket['ask_prem'] >= 500_000 and bucket['trades'] >= 3:
        prem_score = min(30, prem_score + 10)
    if bucket['ask_dominance'] < 0.65:
        prem_score = max(0, prem_score - 8)
    base += prem_score

    # Regime
    if regime == "CHOP": base -= 15
    if (bucket['type'] == 'CALL' and regime == 'TREND_UP') or (bucket['type'] == 'PUT' and regime == 'TREND_DOWN'):
        base += 10

    return base


# ---------- Structure / watch / outcome (mirrors v4_meta_engine) ----------
def evaluate_structure(df_hourly, trade_type, spot):
    smc_score = 0; is_pb = False; smc_sl = 0
    brk_score = 0; is_brk = False; brk_sl = 0
    pb_zone = None; brk_level = None
    try:
        if df_hourly is None or df_hourly.empty: return (0, False, 0, 0, False, 0, None, None)
        recent_20 = df_hourly.tail(20)
        recent_5 = df_hourly.tail(5)
        df_smc = detect_fvg(df_hourly)
        rec_smc = df_smc.tail(40)

        if trade_type == "CALL":
            bull = rec_smc[rec_smc['Bull_FVG'] == True]
            if not bull.empty:
                fbot = float(bull['FVG_Bull_Bot'].iloc[-1])
                ftop = float(bull['FVG_Bull_Top'].iloc[-1])
                pb_zone = (fbot, ftop); smc_sl = fbot * 0.99
                if fbot * 0.98 <= spot <= ftop * 1.02:
                    is_pb = True
                    d = abs(spot - (fbot + ftop) / 2) / spot
                    smc_score += 20 if d < 0.005 else (10 if d < 0.01 else 0)
        else:
            bear = rec_smc[rec_smc['Bear_FVG'] == True]
            if not bear.empty:
                fbot = float(bear['FVG_Bear_Bot'].iloc[-1])
                ftop = float(bear['FVG_Bear_Top'].iloc[-1])
                pb_zone = (fbot, ftop); smc_sl = ftop * 1.01
                if fbot * 0.98 <= spot <= ftop * 1.02:
                    is_pb = True
                    d = abs(spot - (fbot + ftop) / 2) / spot
                    smc_score += 20 if d < 0.005 else (10 if d < 0.01 else 0)

        avg_v = float(recent_20['Volume'].mean()) if 'Volume' in recent_20.columns else 0
        cur_v = float(recent_5['Volume'].iloc[-1]) if 'Volume' in recent_5.columns else 0
        vol_conf = avg_v > 0 and cur_v > avg_v * 1.5

        if trade_type == "CALL":
            ceiling = float(recent_20['High'].max()); brk_level = ceiling
            rt = float(recent_5['High'].max()); rb = float(recent_5['Low'].min())
            if (rt - rb) / spot < 0.01: brk_score += 10
            if int((recent_20['High'] >= ceiling * 0.998).sum()) >= 2: brk_score += 10
            cb_c = float(recent_5['Close'].iloc[-1]); cb_o = float(recent_5['Open'].iloc[-1])
            if cb_c > ceiling and cb_c > cb_o and spot > ceiling * 1.002:
                if vol_conf: is_brk = True; brk_score += 20; brk_sl = ceiling * 0.99
                else: brk_score += 8
        else:
            support = float(recent_20['Low'].min()); brk_level = support
            rt = float(recent_5['High'].max()); rb = float(recent_5['Low'].min())
            if (rt - rb) / spot < 0.01: brk_score += 10
            if int((recent_20['Low'] <= support * 1.002).sum()) >= 2: brk_score += 10
            cb_c = float(recent_5['Close'].iloc[-1]); cb_o = float(recent_5['Open'].iloc[-1])
            if cb_c < support and cb_c < cb_o and spot < support * 0.998:
                if vol_conf: is_brk = True; brk_score += 20; brk_sl = support * 1.01
                else: brk_score += 8
    except: pass
    return (smc_score, is_pb, smc_sl, brk_score, is_brk, brk_sl, pb_zone, brk_level)


def simulate_watch(intraday_5m, watch_ts, zone_low, zone_high, ttype, path, rth_end, dte=7):
    if intraday_5m is None or intraday_5m.empty: return None, "no_intraday_data"
    bars = intraday_5m[intraday_5m.index >= watch_ts]
    if bars.empty: return None, "no_post_watch_bars"
    max_age_h = 1.0 if dte <= 7 else 4.0 if dte <= 14 else 12.0
    expiry_ts = watch_ts + timedelta(hours=max_age_h)
    for ts, bar in bars.iterrows():
        if ts > expiry_ts: return None, "expired"
        if ts > rth_end: return None, "rth_close_no_confirm"
        high = float(bar['High']); low = float(bar['Low'])
        o = float(bar['Open']); c = float(bar['Close'])
        rng = high - low
        if rng <= 0: continue
        if path == 'PULLBACK':
            entered = low <= zone_high and high >= zone_low
            if not entered: continue
            if ttype == 'CALL':
                if (min(o, c) - low) / rng > 0.4 and c > o:
                    return ts, f"bull_rejection@{c:.2f}"
            else:
                if (high - max(o, c)) / rng > 0.4 and c < o:
                    return ts, f"bear_rejection@{c:.2f}"
        else:
            level = (zone_low + zone_high) / 2
            if ttype == 'CALL':
                if c > level and (c - o) / rng > 0.5:
                    return ts, f"breakout_confirm@{c:.2f}"
            else:
                if c < level and (o - c) / rng > 0.5:
                    return ts, f"breakdown_confirm@{c:.2f}"
    return None, "no_confirm_in_window"


def measure_outcome(intraday_5m, entry_ts, ttype, rth_end):
    if intraday_5m is None or intraday_5m.empty: return None, None, None
    bars = intraday_5m[intraday_5m.index >= entry_ts]
    if bars.empty: return None, None, None
    entry = float(bars['Close'].iloc[0])
    rth_bars = bars[bars.index <= rth_end]
    if rth_bars.empty: rth_bars = bars
    exit_p = float(rth_bars['Close'].iloc[-1])
    high = float(rth_bars['High'].max()); low = float(rth_bars['Low'].min())
    sign = 1 if ttype == 'CALL' else -1
    fwd = sign * (exit_p - entry) / entry
    mfe = sign * (high - entry) / entry if sign > 0 else sign * (low - entry) / entry
    mae = sign * (low - entry) / entry if sign > 0 else sign * (high - entry) / entry
    return fwd, mfe, mae


# ---------- Main ----------
def backtest_day(ticker, day, regime, df_h, intraday, spy_intraday, rth_start, rth_end):
    """Returns (status, dict) for one (ticker, day) pair."""
    date_str = day.strftime('%Y-%m-%d')
    records = fetch_flow_per_strike(ticker, date_str)
    if not records:
        return "NO_DATA", {"records": 0}

    buckets = aggregate_buckets(records, rth_start, rth_end, bucket_minutes=15)
    if not buckets:
        return "NO_QUALIFYING_BUCKETS", {"records": len(records)}

    # Walk forward through buckets in order; first one whose total score (base+structure) ≥ threshold wins
    for b in buckets:
        base = score_bucket(b, regime)
        # Spot at bucket time
        if intraday is not None and not intraday.empty:
            prior = intraday[intraday.index <= b['ts']]
            spot = float(prior['Close'].iloc[-1]) if not prior.empty else 0
        else:
            spot = 0
        if spot <= 0: continue

        df_h_slice = df_h[df_h.index <= b['ts']] if df_h is not None and not df_h.empty else df_h
        smc_s, is_pb, smc_sl, brk_s, is_brk, brk_sl, pb_zone, brk_level = evaluate_structure(df_h_slice, b['type'], spot)

        pb_total = base + smc_s
        brk_total = base + brk_s

        chosen = None
        if is_brk and brk_total >= SCORE_THRESHOLD and brk_level is not None:
            chosen = ('BREAKOUT', brk_total, (brk_level * 0.997, brk_level * 1.003), brk_sl)
        elif is_pb and pb_total >= SCORE_THRESHOLD and pb_zone is not None:
            chosen = ('PULLBACK', pb_total, pb_zone, smc_sl)

        if not chosen: continue
        path, score, zone, sl = chosen

        entry_ts, reason = simulate_watch(intraday, b['ts'], zone[0], zone[1], b['type'], path, rth_end)
        out = {"watch_ts": b['ts'].strftime('%H:%M'), "score": round(score, 1), "type": b['type'],
               "path": path, "ask_prem": round(b['ask_prem'], 0), "ask_dom": round(b['ask_dominance'], 2),
               "spot_at_watch": round(spot, 2)}
        if entry_ts is None:
            out["status"] = f"WATCH_{path}_EXPIRED"; out["expire_reason"] = reason
            return f"WATCH_{path}_EXPIRED", out
        out["entry_ts"] = entry_ts.strftime('%H:%M'); out["entry_reason"] = reason
        fwd, mfe, mae = measure_outcome(intraday, entry_ts, b['type'], rth_end)
        if fwd is not None:
            out["fwd_pct"] = round(fwd * 100, 2); out["mfe_pct"] = round(mfe * 100, 2); out["mae_pct"] = round(mae * 100, 2)
            spy_fwd, _, _ = measure_outcome(spy_intraday, entry_ts, 'CALL', rth_end)
            if spy_fwd is not None:
                out["spy_fwd_pct"] = round(spy_fwd * 100, 2)
                out["excess_pct"] = round((fwd - spy_fwd) * 100, 2)
        return f"TRIGGER_{path}", out

    # No bucket crossed threshold
    return "REJECTED_LOW_SCORE", {"buckets_evaluated": len(buckets), "best_base_score": max((score_bucket(b, regime) for b in buckets), default=0)}


def main():
    end_date = datetime.utcnow().date()
    # If we're before 20:30Z today, last completed session is yesterday
    if datetime.utcnow().time() < dtime(20, 30):
        end_date -= timedelta(days=1)
    while end_date.weekday() >= 5: end_date -= timedelta(days=1)
    days = trading_days(end_date, DAYS_BACK)
    print(f"\n{'='*78}")
    print(f"V4 WEEK BACKTEST | {days[0]} → {days[-1]} ({len(days)} sessions)")
    print(f"{'='*78}")

    with open(WATCHLIST_PATH) as f:
        watchlist = list(json.load(f).keys())
    print(f"Watchlist: {', '.join(watchlist)}")
    print(f"Score threshold: {SCORE_THRESHOLD} | Min bucket premium: ${MIN_BUCKET_PREMIUM:,}")

    all_results = []  # flat list of (date, ticker, status, detail)

    for day in days:
        rth_start, rth_end = rth_bounds(day)
        print(f"\n--- {day} ---")
        regime = determine_regime_at(rth_end)
        spy_intraday = fetch_session_5min('SPY', rth_start, rth_end)
        print(f"  Regime: {regime}")

        # Per-ticker (parallel-safe via day; sequential is fine given small N)
        for ticker in watchlist:
            df_h = fetch_25d_hourly(ticker, rth_end)
            intraday = fetch_session_5min(ticker, rth_start, rth_end)
            status, detail = backtest_day(ticker, day, regime, df_h, intraday, spy_intraday, rth_start, rth_end)
            all_results.append({"date": str(day), "ticker": ticker, "regime": regime, "status": status, **detail})
            tag = "🟢" if status.startswith("TRIGGER_") else ("👁️" if "WATCH" in status else "·")
            extra = ""
            if status.startswith("TRIGGER_"):
                extra = f" score={detail.get('score')} entry={detail.get('entry_ts','?')} fwd={detail.get('fwd_pct','?')}% excess={detail.get('excess_pct','?')}%"
            elif "WATCH" in status:
                extra = f" score={detail.get('score')} reason={detail.get('expire_reason','?')}"
            print(f"  {tag} {ticker}: {status}{extra}")

    # ---------- Summary ----------
    print(f"\n{'='*78}")
    print(f"WEEK SUMMARY — UW calls used: {_uw_calls}")
    print(f"{'='*78}")

    triggers = [r for r in all_results if r['status'].startswith('TRIGGER_')]
    watches_exp = [r for r in all_results if 'EXPIRED' in r['status']]
    rejects = [r for r in all_results if r['status'].startswith('REJECTED_') or r['status'] == 'NO_DATA' or r['status'] == 'NO_QUALIFYING_BUCKETS']

    print(f"\nAcross {len(days)} sessions × {len(watchlist)} tickers = {len(all_results)} total scans:")
    print(f"  Triggers fired: {len(triggers)}")
    print(f"  Watches expired (scored but no SMC retest): {len(watches_exp)}")
    print(f"  Rejected: {len(rejects)}")

    if triggers:
        wins = [t for t in triggers if t.get('fwd_pct', 0) > 0.5]
        losses = [t for t in triggers if t.get('fwd_pct', 0) < -0.5]
        flat = [t for t in triggers if t not in wins and t not in losses]
        avg_fwd = sum(t.get('fwd_pct', 0) for t in triggers) / len(triggers)
        excess_vals = [t.get('excess_pct') for t in triggers if t.get('excess_pct') is not None]
        avg_excess = sum(excess_vals) / len(excess_vals) if excess_vals else 0
        print(f"\nTrigger outcomes (fwd-to-RTH-close, 0.5% W/L threshold):")
        print(f"  W/L/Flat: {len(wins)}/{len(losses)}/{len(flat)} | Win rate: {len(wins)/(len(wins)+len(losses))*100 if (wins or losses) else 0:.1f}% (excl. flat)")
        print(f"  Avg fwd: {avg_fwd:+.2f}% | Avg excess vs SPY: {avg_excess:+.2f}%")
        print(f"\nPath breakdown:")
        from collections import Counter
        for p, n in Counter(t['path'] for t in triggers).items():
            sub = [t for t in triggers if t['path'] == p]
            sub_fwd = sum(t.get('fwd_pct', 0) for t in sub) / len(sub) if sub else 0
            print(f"  {p}: {n} | avg fwd {sub_fwd:+.2f}%")
        print(f"\nType breakdown:")
        for tt, n in Counter(t['type'] for t in triggers).items():
            sub = [t for t in triggers if t['type'] == tt]
            sub_fwd = sum(t.get('fwd_pct', 0) for t in sub) / len(sub) if sub else 0
            print(f"  {tt}: {n} | avg fwd {sub_fwd:+.2f}%")

    print(f"\nRejection breakdown:")
    from collections import Counter
    for s, n in Counter(r['status'] for r in rejects).most_common():
        print(f"  {s}: {n}")

    # Persist
    with open(RESULTS_PATH, 'w') as f:
        json.dump({
            'sessions': [str(d) for d in days],
            'watchlist': watchlist,
            'score_threshold': SCORE_THRESHOLD,
            'uw_calls': _uw_calls,
            'uw_429_retries': _uw_429s,
            'caveats': {
                'greeks_iv_darkpool': 'snapshot-only endpoints — skipped in backtest scoring',
                'flow_data_source': '/api/stock/{ticker}/flow-per-strike (per-strike per-15min aggregations)',
                'differs_from_live_engine': True,
            },
            'results': all_results,
        }, f, indent=2, default=str)
    print(f"\n💾 Results: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
