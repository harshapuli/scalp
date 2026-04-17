"""
V4 BACKTEST — today's RTH session, walk-forward over flow alerts.

What this does:
- Pulls today's UW flow alerts (with created_at) and dark-pool prints for the
  active watchlist, plus Alpaca 1Hour (25d) and 5Min (today RTH) bars.
- Replays each ticker's alert stream chronologically, building clusters with
  the same logic as v4_meta_engine.
- At the moment a cluster crosses the engine's gates and scores >=40, it
  becomes a WATCH. The harness then walks forward through 5-min bars looking
  for the SMC retest confirmation (rejection wick on FVG zone, or strong-body
  breakout-confirm above the level). First confirming bar = entry.
- Outcome is measured from entry close to RTH close, sign-adjusted for PUT.
- SPY baseline is computed over the same window for excess-return reporting.

Caveats (called out here so they're not mistaken for capability):
- /api/stock/{ticker}/greeks and /api/stock/{ticker}/iv-rank return the latest
  snapshot only. Backtest uses these as proxies for "today's reading" — they
  don't time-travel within the session.
- /api/darkpool/{ticker} returns today's prints; we treat them as if available
  at the cluster timestamp (mild forward-looking bias on confidence score).
- Position sizing isn't simulated; outcomes are raw underlying % move.
"""
import os
import sys
import json
import math
import time
import requests
from datetime import datetime, timedelta, time as dtime, timezone
from collections import defaultdict

UTC = timezone.utc

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars
from swing_trade_strategy.smc_engine import detect_fvg

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

UW_API_KEY = os.getenv("UW_API_KEY", "").replace('"', '')
HEADERS = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
BASE_URL = "https://api.unusualwhales.com"

# ---------- Config ----------
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', 'ranked_watchlist.json')
RESULTS_PATH = os.path.join(os.path.dirname(__file__), 'v4_backtest_today.json')

# Today's most-recently-completed RTH session (UTC).
# If we're between 00:00Z and ~13:00Z, "today's RTH" is technically yesterday's session.
def resolve_session_date():
    now = datetime.utcnow()
    candidate = now.date()
    # If we're before today's RTH close, look at yesterday
    if now.time() < dtime(20, 30):
        candidate = now.date() - timedelta(days=1)
    # Skip back over weekends
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate

SESSION_DATE = resolve_session_date()
RTH_START = datetime.combine(SESSION_DATE, dtime(13, 30), tzinfo=UTC)
RTH_END = datetime.combine(SESSION_DATE, dtime(20, 0), tzinfo=UTC)

# ---------- Rate limiting (200/min) ----------
_api_calls = 0
_last_call_ts = 0.0
def _api_call():
    global _api_calls, _last_call_ts
    # Pace at ~3 calls/sec = 180/min, leaving headroom under the 200/min cap
    elapsed = time.time() - _last_call_ts
    if elapsed < 0.35:
        time.sleep(0.35 - elapsed)
    _last_call_ts = time.time()
    _api_calls += 1


def _get(url, timeout=10):
    _api_call()
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  ⚠️ API error {url[:80]}: {e}")
    return None


# ---------- Data fetchers ----------
def fetch_flow_alerts(ticker: str) -> list:
    # CRITICAL: UW uses `ticker_symbol`, not `ticker`. The `ticker` param is silently ignored
    # and returns the global stream — that bug exists in live v4_meta_engine.py too.
    j = _get(f"{BASE_URL}/api/option-trades/flow-alerts?limit=500&ticker_symbol={ticker}")
    if not j: return []
    alerts = j.get('data', [])
    out = []
    for a in alerts:
        # Defense-in-depth: drop any alert whose ticker doesn't match (in case the API ever changes)
        if a.get('ticker', '').upper() != ticker.upper():
            continue
        ts = a.get('created_at', '')
        try:
            t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            if RTH_START <= t <= RTH_END:
                a['_ts'] = t
                out.append(a)
        except:
            continue
    out.sort(key=lambda a: a['_ts'])
    return out


def fetch_darkpool(ticker: str) -> list:
    j = _get(f"{BASE_URL}/api/darkpool/{ticker}?limit=200")
    return j.get('data', []) if j else []


def fetch_greeks_snapshot(ticker: str) -> float:
    j = _get(f"{BASE_URL}/api/stock/{ticker}/greeks")
    if not j or not j.get('data'): return 0.0
    try:
        return float(j['data'][-1].get("gamma_per_one_percent_move_dir", 0))
    except: return 0.0


def fetch_iv_rank(ticker: str) -> float:
    j = _get(f"{BASE_URL}/api/stock/{ticker}/iv-rank")
    if not j or not j.get('data'): return 50.0
    try:
        return float(j['data'][-1].get('iv_rank_1y', 50))
    except: return 50.0


def fetch_session_5min(ticker: str):
    # Pull from session start to a couple hours past close so we can measure outcomes
    return fetch_alpaca_bars(
        ticker, '5Min',
        RTH_START.strftime('%Y-%m-%dT%H:%M:%SZ'),
        (RTH_END + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    )


def fetch_25d_hourly(ticker: str):
    return fetch_alpaca_bars(
        ticker, '1Hour',
        (RTH_END - timedelta(days=25)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        RTH_END.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )


def determine_regime() -> str:
    df = fetch_alpaca_bars(
        'SPY', '1D',
        (RTH_END - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        RTH_END.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    if df is None or df.empty or len(df) <= 20:
        return "CHOP"
    sma20 = df['Close'].rolling(20).mean().iloc[-1]
    close = df['Close'].iloc[-1]
    if close > sma20 * 1.01: return "TREND_UP"
    if close < sma20 * 0.99: return "TREND_DOWN"
    return "CHOP"


# ---------- Engine logic (mirrors v4_meta_engine, time-aware) ----------
def build_clusters_at(alerts: list, as_of: datetime) -> dict:
    """Return clusters built from all alerts with _ts <= as_of."""
    clusters = {}
    for a in alerts:
        if a['_ts'] > as_of:
            break
        try:
            vol = float(a.get('volume', 0))
            oi = float(a.get('open_interest', 0))
            ask = float(a.get('ask', 0.1))
            bid = float(a.get('bid', 0.1))
            spot = float(a.get('underlying_price', 0))
            strike = float(a.get('strike', 0))
            expiry = a.get('expiry')
            t = a.get('type', 'CALL').upper()
            premium = float(a.get('total_premium', 0))
        except:
            continue

        mid = max((ask + bid) / 2, 0.01)
        spread_pct = abs(ask - bid) / mid
        if expiry:
            try:
                _exp_dt = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=UTC)
                _dte = (_exp_dt - as_of).days
            except:
                _dte = 7
        else:
            _dte = 7
        max_spread = 0.08 if _dte < 10 else 0.12
        if spread_pct > max_spread: continue

        ask_prem = float(a.get('total_ask_side_prem', 0))
        bid_prem = float(a.get('total_bid_side_prem', 0))
        ask_dom = ask_prem / max(ask_prem + bid_prem, 1)
        side = "ASK" if ask_prem > bid_prem else "BID"

        if expiry and spot > 0:
            dist = abs(strike - spot) / spot
            if _dte <= 10 and dist > 0.05: continue
            if _dte <= 21 and dist > 0.08: continue
            if _dte > 21 and dist > 0.12: continue

        # Time-of-day score (use alert's own timestamp)
        est_hr = a['_ts'].hour - 4 + (a['_ts'].minute / 60.0)
        tod_score = 5 if 9.5 <= est_hr < 10.5 else (-5 if 11.5 <= est_hr <= 13.5 else 0)

        # Decay relative to as_of
        delta_min = (as_of - a['_ts']).total_seconds() / 60.0
        time_weight = math.exp(-max(0, delta_min) / 30.0)

        key = f"{t}_{strike}_{side}_{expiry}"
        c = clusters.setdefault(key, {
            "type": t, "side": side, "expiry": expiry, "spot": spot, "strike": strike,
            "total_premium": 0.0, "sweep_count": 0, "confirmed_opening": vol > oi,
            "tod_score": tod_score, "spread_penalty": max(0, spread_pct * 10),
            "ask_dominance": ask_dom, "first_ts": a['_ts'], "last_ts": a['_ts'],
        })
        c["total_premium"] += premium * time_weight
        c["sweep_count"] += 1
        c["last_ts"] = a['_ts']
        if vol > oi: c["confirmed_opening"] = True
        if ask_dom > c["ask_dominance"]: c["ask_dominance"] = ask_dom
    return clusters


def select_best_cluster(clusters: dict) -> tuple:
    """Same selection as v4_meta_engine: largest qualifying ASK-side cluster."""
    qualifying = [c for c in clusters.values() if c['total_premium'] >= 100_000]
    if not qualifying: return None, "REJECTED_LOW_PREMIUM"
    asks = [c for c in qualifying if c['side'] == 'ASK']
    if not asks: return None, "REJECTED_BID_SIDE"
    return max(asks, key=lambda x: x['total_premium']), "PROCESSING"


def evaluate_structure(df_hourly, trade_type: str, spot: float):
    """Same return signature as v4_meta_engine.evaluate_technical_structure."""
    smc_score = 0; is_pullback = False; smc_sl = 0
    brk_score = 0; is_breakout = False; brk_sl = 0
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
                fvg_mid = (fbot + ftop) / 2
                if fbot * 0.98 <= spot <= ftop * 1.02:
                    is_pullback = True
                    d = abs(spot - fvg_mid) / spot
                    smc_score += 20 if d < 0.005 else (10 if d < 0.01 else 0)
        else:
            bear = rec_smc[rec_smc['Bear_FVG'] == True]
            if not bear.empty:
                fbot = float(bear['FVG_Bear_Bot'].iloc[-1])
                ftop = float(bear['FVG_Bear_Top'].iloc[-1])
                pb_zone = (fbot, ftop); smc_sl = ftop * 1.01
                fvg_mid = (fbot + ftop) / 2
                if fbot * 0.98 <= spot <= ftop * 1.02:
                    is_pullback = True
                    d = abs(spot - fvg_mid) / spot
                    smc_score += 20 if d < 0.005 else (10 if d < 0.01 else 0)

        avg_v = float(recent_20['Volume'].mean()) if 'Volume' in recent_20.columns else 0
        cur_v = float(recent_5['Volume'].iloc[-1]) if 'Volume' in recent_5.columns else 0
        vol_conf = avg_v > 0 and cur_v > avg_v * 1.5

        if trade_type == "CALL":
            ceiling = float(recent_20['High'].max())
            rt = float(recent_5['High'].max()); rb = float(recent_5['Low'].min())
            brk_level = ceiling
            if (rt - rb) / spot < 0.01: brk_score += 10
            touches = int((recent_20['High'] >= ceiling * 0.998).sum())
            if touches >= 2: brk_score += 10
            cb_close = float(recent_5['Close'].iloc[-1]); cb_open = float(recent_5['Open'].iloc[-1])
            if cb_close > ceiling and cb_close > cb_open and spot > ceiling * 1.002:
                if vol_conf:
                    is_breakout = True; brk_score += 20; brk_sl = ceiling * 0.99
                else:
                    brk_score += 8
        else:
            support = float(recent_20['Low'].min())
            rt = float(recent_5['High'].max()); rb = float(recent_5['Low'].min())
            brk_level = support
            if (rt - rb) / spot < 0.01: brk_score += 10
            touches = int((recent_20['Low'] <= support * 1.002).sum())
            if touches >= 2: brk_score += 10
            cb_close = float(recent_5['Close'].iloc[-1]); cb_open = float(recent_5['Open'].iloc[-1])
            if cb_close < support and cb_close < cb_open and spot < support * 0.998:
                if vol_conf:
                    is_breakout = True; brk_score += 20; brk_sl = support * 1.01
                else:
                    brk_score += 8
    except: pass
    return (smc_score, is_pullback, smc_sl, brk_score, is_breakout, brk_sl, pb_zone, brk_level)


def darkpool_confidence(dp_data: list, trade_type: str) -> float:
    bull = 0; bear = 0
    for t in dp_data:
        try:
            p = float(t.get("price", 0))
            ask = float(t.get("nbbo_ask", p)); bid = float(t.get("nbbo_bid", p))
            prem = p * float(t.get("size", 0))
            if p >= ask: bull += prem
            elif p <= bid: bear += prem
        except: continue
    p_score = math.log10(max(bull, 1)) * 3 if bull > 0 else 0
    n_score = math.log10(max(bear, 1)) * 3 if bear > 0 else 0
    if trade_type == "CALL": return p_score - n_score * 0.5
    return n_score - p_score * 0.5


def score_cluster(cluster, regime, gamma_dir, iv_rank, dp_score):
    """Compute base score (mirrors v4_meta_engine)."""
    base = 0
    tod = cluster['tod_score']
    base += tod
    base -= cluster['spread_penalty']

    prem = cluster['total_premium']
    prem_score = min(20, max(0, math.log10(max(prem / 100_000, 1)) * 7))
    if prem >= 500_000 and cluster['sweep_count'] >= 3:
        prem_score = min(30, prem_score + 10)
    if cluster.get('ask_dominance', 1.0) < 0.65:
        prem_score = max(0, prem_score - 8)
    base += prem_score

    if cluster['confirmed_opening']: base += 10
    if regime == "CHOP": base -= 15
    if (cluster['type'] == 'CALL' and regime == 'TREND_UP') or (cluster['type'] == 'PUT' and regime == 'TREND_DOWN'):
        base += 10

    # Greeks
    if gamma_dir > 0:
        base += 10
    elif gamma_dir < 0:
        if (cluster['type'] == "CALL" and regime == "TREND_UP") or (cluster['type'] == "PUT" and regime == "TREND_DOWN"):
            pass  # no penalty in aligned trend
        else:
            base -= 10

    # IV penalty (proxy)
    if cluster['expiry']:
        exp = datetime.strptime(cluster['expiry'], "%Y-%m-%d").replace(tzinfo=UTC)
        dte = (exp - cluster['first_ts']).days
    else:
        dte = 7
    if iv_rank > 70:
        if dte < 10: return None  # hard reject
        base -= (iv_rank - 50) * 0.3

    base += dp_score
    return base


# ---------- Watch lifecycle simulation ----------
def simulate_watch(intraday_5m, watch_created_at, zone_low, zone_high, ttype, path, dte):
    """Walk forward through 5min bars from watch_created_at; return (entry_ts, reason) or (None, expire_reason)."""
    if intraday_5m is None or intraday_5m.empty: return None, "no_intraday_data"
    bars = intraday_5m[intraday_5m.index >= watch_created_at]
    if bars.empty: return None, "no_post_watch_bars"

    # Watch expiry per DTE
    max_age_h = 1.0 if dte <= 7 else 4.0 if dte <= 14 else 12.0
    expiry_ts = watch_created_at + timedelta(hours=max_age_h)

    for ts, bar in bars.iterrows():
        if ts > expiry_ts: return None, "expired"
        if ts > RTH_END: return None, "rth_close_no_confirm"

        high = float(bar['High']); low = float(bar['Low'])
        o = float(bar['Open']); c = float(bar['Close'])
        rng = high - low
        if rng <= 0: continue

        if path == 'PULLBACK':
            entered = low <= zone_high and high >= zone_low
            if not entered: continue
            if ttype == 'CALL':
                lower_wick = min(o, c) - low
                if lower_wick / rng > 0.4 and c > o:
                    return ts, f"bull_rejection@{c:.2f}"
            else:
                upper_wick = high - max(o, c)
                if upper_wick / rng > 0.4 and c < o:
                    return ts, f"bear_rejection@{c:.2f}"
        else:  # BREAKOUT
            level = (zone_low + zone_high) / 2
            if ttype == 'CALL':
                body = (c - o) / rng
                if c > level and body > 0.5:
                    return ts, f"breakout_confirm@{c:.2f}"
            else:
                body = (o - c) / rng
                if c < level and body > 0.5:
                    return ts, f"breakdown_confirm@{c:.2f}"
    return None, "no_confirm_in_window"


def measure_outcome(intraday_5m, entry_ts, ttype):
    """From entry close to RTH close, raw underlying %."""
    if intraday_5m is None or intraday_5m.empty: return None, None, None
    bars = intraday_5m[intraday_5m.index >= entry_ts]
    if bars.empty: return None, None, None
    entry_price = float(bars['Close'].iloc[0])
    # Restrict outcome window to within RTH for clean measurement
    rth_bars = bars[bars.index <= RTH_END]
    if rth_bars.empty: rth_bars = bars
    exit_price = float(rth_bars['Close'].iloc[-1])
    high = float(rth_bars['High'].max()); low = float(rth_bars['Low'].min())
    sign = 1 if ttype == 'CALL' else -1
    fwd = sign * (exit_price - entry_price) / entry_price
    mfe = sign * (high - entry_price) / entry_price if sign > 0 else sign * (low - entry_price) / entry_price
    mae = sign * (low - entry_price) / entry_price if sign > 0 else sign * (high - entry_price) / entry_price
    return fwd, mfe, mae


# ---------- Main ----------
def main():
    print(f"\n{'='*78}")
    print(f"V4 BACKTEST | session date: {SESSION_DATE} | RTH {RTH_START.time()}–{RTH_END.time()} UTC")
    print(f"{'='*78}\n")

    if not os.path.exists(WATCHLIST_PATH):
        print("No watchlist.")
        return
    with open(WATCHLIST_PATH) as f:
        watchlist = list(json.load(f).keys())
    print(f"Watchlist ({len(watchlist)}): {', '.join(watchlist)}")

    print("\nFetching SPY context (regime + baseline)...")
    regime = determine_regime()
    spy_5m = fetch_session_5min('SPY')
    print(f"  Regime: {regime}")

    results = []
    for ticker in watchlist:
        print(f"\n— {ticker} —")
        alerts = fetch_flow_alerts(ticker)
        if not alerts:
            print("  no flow alerts in session"); results.append({"ticker": ticker, "status": "NO_FLOW"}); continue
        print(f"  {len(alerts)} flow alerts in session")

        dp = fetch_darkpool(ticker)
        gamma_dir = fetch_greeks_snapshot(ticker)
        iv_rank = fetch_iv_rank(ticker)
        df_h = fetch_25d_hourly(ticker)
        intraday = fetch_session_5min(ticker)

        # Walk forward: at each unique alert timestamp, rebuild clusters and try to qualify
        triggered = False
        for cutoff_idx in range(len(alerts)):
            as_of = alerts[cutoff_idx]['_ts']
            clusters = build_clusters_at(alerts, as_of)
            best, status = select_best_cluster(clusters)
            if not best: continue

            # Determine spot at this moment from intraday bars
            if intraday is not None and not intraday.empty:
                prior = intraday[intraday.index <= as_of]
                spot_now = float(prior['Close'].iloc[-1]) if not prior.empty else best['spot']
            else:
                spot_now = best['spot']

            dp_score = darkpool_confidence(dp, best['type'])
            base = score_cluster(best, regime, gamma_dir, iv_rank, dp_score)
            if base is None:
                results.append({"ticker": ticker, "status": "REJECTED_IV_CRUSH", "first_seen": str(as_of)})
                triggered = True; break  # one final verdict per ticker

            # Slice 1H df to bars up to as_of for honest historical structure eval
            df_h_slice = df_h[df_h.index <= as_of] if df_h is not None and not df_h.empty else df_h
            smc_s, is_pb, smc_sl, brk_s, is_brk, brk_sl, pb_zone, brk_level = evaluate_structure(df_h_slice, best['type'], spot_now)

            pb_total = base + smc_s
            brk_total = base + brk_s

            chosen_path = None; chosen_score = 0; zone = None; sl = 0
            if is_brk and brk_total >= 40 and brk_level is not None:
                chosen_path = 'BREAKOUT'; chosen_score = brk_total
                zone = (brk_level * 0.997, brk_level * 1.003); sl = brk_sl
            elif is_pb and pb_total >= 40 and pb_zone is not None:
                chosen_path = 'PULLBACK'; chosen_score = pb_total
                zone = pb_zone; sl = smc_sl

            if chosen_path is None: continue  # below threshold, keep walking forward

            # Watch created at as_of — simulate retest
            if best['expiry']:
                _exp = datetime.strptime(best['expiry'], "%Y-%m-%d").replace(tzinfo=UTC)
                dte = (_exp - as_of).days
            else:
                dte = 7
            entry_ts, reason = simulate_watch(intraday, as_of, zone[0], zone[1], best['type'], chosen_path, dte)
            outcome = {"ticker": ticker, "status": f"WATCH_{chosen_path}",
                       "score": round(chosen_score, 1), "type": best['type'],
                       "watch_created": as_of.strftime('%H:%M'), "dte": dte, "spot_at_watch": round(spot_now, 2),
                       "premium_clustered": round(best['total_premium'], 0),
                       "ask_dominance": round(best.get('ask_dominance', 0), 2),
                       "smc_score": smc_s, "brk_score": brk_s, "dp_score": round(dp_score, 1),
                       "regime": regime, "gamma_dir": gamma_dir, "iv_rank": iv_rank}

            if entry_ts is None:
                outcome["status"] = f"WATCH_{chosen_path}_EXPIRED"
                outcome["expire_reason"] = reason
                print(f"  👁️ WATCH_{chosen_path} score={chosen_score:.1f} @ {as_of.strftime('%H:%M')} → {reason}")
            else:
                outcome["status"] = f"TRIGGER_{chosen_path}_CONFIRMED"
                outcome["entry_ts"] = entry_ts.strftime('%H:%M')
                outcome["entry_reason"] = reason
                fwd, mfe, mae = measure_outcome(intraday, entry_ts, best['type'])
                if fwd is not None:
                    outcome["fwd_to_close_pct"] = round(fwd * 100, 2)
                    outcome["mfe_pct"] = round(mfe * 100, 2)
                    outcome["mae_pct"] = round(mae * 100, 2)
                    # SPY baseline over same window
                    spy_fwd, _, _ = measure_outcome(spy_5m, entry_ts, 'CALL')
                    if spy_fwd is not None:
                        outcome["spy_fwd_pct"] = round(spy_fwd * 100, 2)
                        outcome["excess_pct"] = round((fwd - spy_fwd) * 100, 2)
                tag = "🟢" if (fwd or 0) > 0.005 else ("🔴" if (fwd or 0) < -0.005 else "⚪")
                print(f"  {tag} TRIGGER_{chosen_path} score={chosen_score:.1f} entry={entry_ts.strftime('%H:%M')} fwd={outcome.get('fwd_to_close_pct','?')}% excess={outcome.get('excess_pct','?')}%")

            results.append(outcome)
            triggered = True
            break

        if not triggered:
            # Use the final cluster decision as the rejection reason
            clusters_final = build_clusters_at(alerts, RTH_END)
            best, status = select_best_cluster(clusters_final)
            if not best:
                print(f"  ❌ {status}")
                results.append({"ticker": ticker, "status": status})
            else:
                # Made it through gates but never scored ≥40
                results.append({"ticker": ticker, "status": "REJECTED_LOW_SCORE", "type": best['type']})
                print(f"  ❌ REJECTED_LOW_SCORE")

    # ---------- Summary ----------
    print(f"\n{'='*78}")
    print(f"BACKTEST SUMMARY ({SESSION_DATE}) — API calls used: {_api_calls}")
    print(f"{'='*78}")
    triggers = [r for r in results if r.get('status', '').startswith('TRIGGER_')]
    watches_expired = [r for r in results if 'EXPIRED' in r.get('status', '')]
    rejections = [r for r in results if r.get('status', '').startswith('REJECTED_') or r.get('status') == 'NO_FLOW']

    print(f"\nTriggers: {len(triggers)} | Watches expired: {len(watches_expired)} | Rejections: {len(rejections)}")

    if triggers:
        wins = sum(1 for t in triggers if t.get('fwd_to_close_pct', 0) > 0.5)
        losses = sum(1 for t in triggers if t.get('fwd_to_close_pct', 0) < -0.5)
        flat = len(triggers) - wins - losses
        avg_fwd = sum(t.get('fwd_to_close_pct', 0) for t in triggers) / len(triggers)
        avg_excess = sum(t.get('excess_pct', 0) for t in triggers if t.get('excess_pct') is not None) / max(1, sum(1 for t in triggers if t.get('excess_pct') is not None))
        print(f"  W/L/F: {wins}/{losses}/{flat}")
        print(f"  Avg fwd-to-close: {avg_fwd:+.2f}% | Avg excess vs SPY: {avg_excess:+.2f}%")

    if rejections:
        from collections import Counter
        c = Counter(r['status'] for r in rejections)
        print(f"\nRejection breakdown:")
        for reason, n in c.most_common():
            print(f"  {reason}: {n}")

    # Persist
    with open(RESULTS_PATH, 'w') as f:
        json.dump({
            'session_date': str(SESSION_DATE),
            'rth_start_utc': RTH_START.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'rth_end_utc': RTH_END.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'regime': regime,
            'api_calls': _api_calls,
            'results': results,
        }, f, indent=2, default=str)
    print(f"\n💾 Results: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
