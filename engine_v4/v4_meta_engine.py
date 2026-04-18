import os
import requests
import json
import math
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars
from swing_trade_strategy.smc_engine import detect_fvg

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

# Pipeline cadence (must match v4_armada.py's sleep interval).
# Used to publish `next_run` in signals.json so the dashboard countdown works.
SCAN_INTERVAL_SECONDS = 300

UW_API_KEY = os.getenv("UW_API_KEY", "").replace('"', '')
HEADERS = {
    "Authorization": f"Bearer {UW_API_KEY}",
    "Accept": "application/json"
}
BASE_URL = "https://api.unusualwhales.com"

def load_watchlist() -> dict:
    """Merge institutional_scanner output (Tier 2: dynamic hot-flow) with
    pre-position CONFIRMED/APPROACHING/TRIGGERED candidates (Tier 1: multi-day buildup).
    Ensures V4 evaluates the names that the pre-position scanner already flagged."""
    watchlist = {}

    # Tier 1 — pre-position candidates that are active today
    prep_path = os.path.join(os.path.dirname(__file__), 'v4_preposition_watchlist.json')
    if os.path.exists(prep_path):
        try:
            with open(prep_path) as f:
                prep = json.load(f)
            for c in prep.get('candidates', []):
                status = c.get('trigger_status', 'WAITING')
                if status in ('CONFIRMED', 'APPROACHING', 'TRIGGERED'):
                    watchlist[c['ticker']] = {
                        'screener_logic': f"Pre-position {status} | {c.get('narrative', '')[:120]}",
                        '_source': 'preposition',
                    }
        except Exception:
            pass

    # Tier 2 — dynamic scanner output (dedupes against Tier 1)
    filepath = os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', 'ranked_watchlist.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                scanner_out = json.load(f)
            for ticker, info in scanner_out.items():
                if ticker not in watchlist:
                    info['_source'] = 'scanner'
                    watchlist[ticker] = info
        except Exception:
            pass

    return watchlist

def determine_market_regime() -> str:
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=60)
        df = fetch_alpaca_bars('SPY', '1D', start.strftime('%Y-%m-%dT%H:%M:%SZ'), end.strftime('%Y-%m-%dT%H:%M:%SZ'))
        if not df.empty and len(df) > 20:
            sma20 = df['Close'].rolling(20).mean().iloc[-1]
            close = df['Close'].iloc[-1]
            if close > sma20 * 1.01: return "TREND_UP"
            elif close < sma20 * 0.99: return "TREND_DOWN" 
    except: pass
    return "CHOP"

def extract_clustered_flow(ticker: str) -> tuple:
    try:
        # UW expects ticker_symbol, not ticker. Using `ticker` returns the global stream
        # (cross-ticker noise misattributed to this ticker — silent data corruption bug).
        url = f"{BASE_URL}/api/option-trades/flow-alerts?limit=50&ticker_symbol={ticker}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', [])
            clusters = {}
            for alert in data:
                vol = float(alert.get('volume', 0))
                oi = float(alert.get('open_interest', 0))
                ask = float(alert.get('ask', 0.1))
                bid = float(alert.get('bid', 0.1))
                spot = float(alert.get('underlying_price', 0))
                strike = float(alert.get('strike', 0))
                expiry = alert.get('expiry')
                t = alert.get('type', 'CALL').upper()
                premium = float(alert.get('total_premium', 0))
                
                mid = max((ask + bid) / 2, 0.01)
                spread_pct = abs(ask - bid) / mid
                # DTE-scaled spread gate: tighter for short-dated where slippage compounds harder
                if expiry:
                    _dte_chk = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
                    max_spread = 0.08 if _dte_chk < 10 else 0.12
                else:
                    max_spread = 0.12
                if spread_pct > max_spread: continue

                # Aggressive-side classification with strength ratio (not binary)
                ask_prem = float(alert.get('total_ask_side_prem', 0))
                bid_prem = float(alert.get('total_bid_side_prem', 0))
                ask_dominance = ask_prem / max(ask_prem + bid_prem, 1)
                side = "ASK" if ask_prem > bid_prem else "BID"

                if expiry and spot > 0:
                    dte = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
                    dist = abs(strike - spot) / spot
                    if dte <= 10 and dist > 0.05: continue
                    if dte <= 21 and dist > 0.08: continue
                    if dte > 21 and dist > 0.12: continue
                
                exec_time = alert.get('created_at', '')
                tod_score = 0
                time_weight = 1.0
                if exec_time:
                    try:
                        utc_dt = datetime.strptime(exec_time[:19], "%Y-%m-%dT%H:%M:%S")
                        est_hr_f = utc_dt.hour - 4 + (utc_dt.minute / 60.0)
                        if 9.5 <= est_hr_f < 10.5:
                            tod_score = 5
                        elif 11.5 <= est_hr_f <= 13.5:
                            tod_score = -5
                            
                        # Institutional decay mapping
                        delta_minutes = (datetime.utcnow() - utc_dt).total_seconds() / 60.0
                        if delta_minutes > 0:
                            time_weight = math.exp(-delta_minutes / 30.0)
                    except: pass

                key = f"{t}_{strike}_{side}_{expiry}"
                if key not in clusters:
                    clusters[key] = {
                        "ticker": ticker, "type": t, "side": side, "expiry": expiry, "spot": spot,
                        "strike": strike,
                        "total_premium": 0, "sweep_count": 0,
                        "confirmed_opening": vol > oi,
                        "tod_score": tod_score,
                        "spread_penalty": max(0, spread_pct * 10),
                        "ask_dominance": ask_dominance,
                        "mid_at_alert": mid,  # per-contract price proxy for TP calc
                    }

                clusters[key]["total_premium"] += premium * time_weight
                clusters[key]["sweep_count"] += 1
                clusters[key]["mid_at_alert"] = mid  # update to latest alert's mid
                if vol > oi: clusters[key]["confirmed_opening"] = True
                if ask_dominance > clusters[key]["ask_dominance"]:
                    clusters[key]["ask_dominance"] = ask_dominance

            if clusters:
                qualifying = [c for c in clusters.values() if c['total_premium'] >= 100_000]
                if not qualifying:
                    return ({}, "REJECTED_LOW_PREMIUM")
                # Prefer the largest ASK-side cluster; only reject the ticker if NO qualifying ASK-side exists
                ask_clusters = [c for c in qualifying if c['side'] == 'ASK']
                if not ask_clusters:
                    return ({}, "REJECTED_BID_SIDE")
                best = max(ask_clusters, key=lambda x: x['total_premium'])
                return (best, "PROCESSING")
    except: pass
    return ({}, "REJECTED_NO_FLOW")

def validate_greeks(ticker: str, trade_type: str, side: str, regime: str) -> tuple:
    try:
        url = f"{BASE_URL}/api/stock/{ticker}/greeks"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200 and res.json().get('data'):
            gamma_dir = float(res.json()['data'][-1].get("gamma_per_one_percent_move_dir", 0))
            if gamma_dir > 0:
                return (True, 10)
            elif gamma_dir < 0:
                if (trade_type == "CALL" and regime == "TREND_UP") or (trade_type == "PUT" and regime == "TREND_DOWN"):
                    return (True, 0)
                else: 
                    return (True, -10)
    except: pass
    return (True, 0)

def validate_iv_rank(ticker: str, dte: int) -> tuple:
    try:
        url = f"{BASE_URL}/api/stock/{ticker}/iv-rank"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200 and res.json().get('data'):
            iv_rank = float(res.json()['data'][-1].get('iv_rank_1y', 50))
            if iv_rank > 70:
                if dte < 10: return (False, 0)
                else: return (True, (iv_rank - 50) * 0.3)
    except: pass
    return (True, 0)

def fetch_darkpool_confidence(ticker: str, trade_type: str) -> float:
    try:
        url = f"{BASE_URL}/api/darkpool/{ticker}?limit=50"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            bull_sum = 0; bear_sum = 0
            for trade in res.json().get("data", []):
                price = float(trade.get("price", 0))
                ask = float(trade.get("nbbo_ask", price))
                bid = float(trade.get("nbbo_bid", price))
                prem = price * float(trade.get("size", 0))
                if price >= ask: bull_sum += prem
                elif price <= bid: bear_sum += prem
            
            p_score = math.log10(max(bull_sum, 1)) * 3 if bull_sum > 0 else 0
            n_score = math.log10(max(bear_sum, 1)) * 3 if bear_sum > 0 else 0
            if trade_type == "CALL": return p_score - (n_score * 0.5)
            else: return n_score - (p_score * 0.5)
    except: pass
    return 0.0

def evaluate_technical_structure(ticker: str, trade_type: str, spot: float) -> tuple:
    try:
        end = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        start = (datetime.utcnow() - timedelta(days=25)).strftime('%Y-%m-%dT%H:%M:%SZ')
        df = fetch_alpaca_bars(ticker, '1Hour', start, end)
        
        if df.empty or len(df) < 25:
             df = fetch_alpaca_bars(ticker, '1D', (datetime.utcnow() - timedelta(days=45)).strftime('%Y-%m-%dT%H:%M:%SZ'), end)
             
        if not df.empty:
            recent_40 = df.tail(40)
            recent_20 = df.tail(20)
            recent_5 = df.tail(5)

            smc_score = 0; is_pullback = False; smc_sl = 0
            brk_score = 0; is_breakout = False; brk_sl = 0
            pb_zone = None  # (low, high) of FVG for entry-trigger watch
            brk_level = None  # ceiling/support for entry-trigger watch

            # PATH A: PULLBACK LOGIC
            df_smc = detect_fvg(df)
            rec_smc = df_smc.tail(40)
            if trade_type == "CALL":
                bull_fvgs = rec_smc[rec_smc['Bull_FVG'] == True]
                if not bull_fvgs.empty:
                    fbot = float(bull_fvgs['FVG_Bull_Bot'].iloc[-1])
                    ftop = float(bull_fvgs['FVG_Bull_Top'].iloc[-1])
                    smc_sl = fbot * 0.99
                    fvg_mid = (fbot + ftop) / 2
                    pb_zone = (fbot, ftop)
                    if fbot * 0.98 <= spot <= ftop * 1.02:
                        is_pullback = True
                        dist_fvg = abs(spot - fvg_mid) / spot
                        if dist_fvg < 0.005: smc_score += 20
                        elif dist_fvg < 0.01: smc_score += 10
            else:
                 bear_fvgs = rec_smc[rec_smc['Bear_FVG'] == True]
                 if not bear_fvgs.empty:
                    fbot = float(bear_fvgs['FVG_Bear_Bot'].iloc[-1])
                    ftop = float(bear_fvgs['FVG_Bear_Top'].iloc[-1])
                    smc_sl = ftop * 1.01
                    fvg_mid = (fbot + ftop) / 2
                    pb_zone = (fbot, ftop)
                    if fbot * 0.98 <= spot <= ftop * 1.02:
                         is_pullback = True
                         dist_fvg = abs(spot - fvg_mid) / spot
                         if dist_fvg < 0.005: smc_score += 20
                         elif dist_fvg < 0.01: smc_score += 10

            # PATH B: BREAKOUT LOGIC (with volume confirmation)
            avg_vol_20 = float(recent_20['Volume'].mean()) if 'Volume' in recent_20.columns and len(recent_20) > 0 else 0
            current_bar_vol = float(recent_5['Volume'].iloc[-1]) if 'Volume' in recent_5.columns else 0
            vol_confirmed = avg_vol_20 > 0 and current_bar_vol > avg_vol_20 * 1.5

            if trade_type == "CALL":
                ceiling = float(recent_20['High'].max())
                range_top = float(recent_5['High'].max())
                range_bot = float(recent_5['Low'].min())
                brk_level = ceiling

                if ((range_top - range_bot) / spot) < 0.01: brk_score += 10
                touches = len(recent_20[recent_20['High'] >= (ceiling * 0.998)])
                if touches >= 2: brk_score += 10

                current_bar_close = float(recent_5['Close'].iloc[-1])
                current_bar_open = float(recent_5['Open'].iloc[-1])
                if current_bar_close > ceiling and (current_bar_close - current_bar_open) > 0:
                    if spot > ceiling * 1.002 and vol_confirmed:
                        is_breakout = True
                        brk_score += 20
                        brk_sl = ceiling * 0.99
                    elif spot > ceiling * 1.002 and not vol_confirmed:
                        # body confirmed but no volume — half credit, no trigger
                        brk_score += 8
            else:
                support = float(recent_20['Low'].min())
                range_top = float(recent_5['High'].max())
                range_bot = float(recent_5['Low'].min())
                brk_level = support

                if ((range_top - range_bot) / spot) < 0.01: brk_score += 10
                touches = len(recent_20[recent_20['Low'] <= (support * 1.002)])
                if touches >= 2: brk_score += 10

                current_bar_close = float(recent_5['Close'].iloc[-1])
                current_bar_open = float(recent_5['Open'].iloc[-1])
                if current_bar_close < support and (current_bar_open - current_bar_close) > 0:
                     if spot < support * 0.998 and vol_confirmed:
                         is_breakout = True
                         brk_score += 20
                         brk_sl = support * 1.01
                     elif spot < support * 0.998 and not vol_confirmed:
                         brk_score += 8

            return (smc_score, is_pullback, smc_sl, brk_score, is_breakout, brk_sl, pb_zone, brk_level)
    except: pass
    return (0, False, 0, 0, False, 0, None, None)

WATCHES_PATH = os.path.join(os.path.dirname(__file__), 'v4_watches.json')


def atomic_write_json(path, data):
    """Write JSON atomically (temp file + rename) — readers never see partial files."""
    import tempfile as _tmp
    dir_path = os.path.dirname(path) or '.'
    fd, tmp = _tmp.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=4, default=str)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
        raise

# DTE-scaled premium TP multipliers (premium-based exits matching trader style).
# Short-dated trades take profit faster — theta is harsher, holding for 80% gain
# rarely materializes. Longer-dated can ride further.
def tp_premium_multiplier(dte: int) -> float:
    if dte <= 7:  return 1.30   # +30% — short-dated, take it quick
    if dte <= 21: return 1.50   # +50% — mid-dated
    return 1.80                 # +80% — longer-dated, ride further

def build_occ_symbol(ticker: str, expiry_str: str, direction: str, strike: float) -> str:
    """Build OCC option symbol: TICKER + YYMMDD + C/P + (strike*1000 as 8-digit int).
    Example: CIFR $18 CALL exp 2026-06-20 → CIFR260620C00018000"""
    try:
        d = datetime.strptime(expiry_str, '%Y-%m-%d').strftime('%y%m%d')
        cp = 'C' if direction.upper() == 'CALL' else 'P'
        strike_int = int(round(strike * 1000))
        return f"{ticker}{d}{cp}{strike_int:08d}"
    except Exception:
        return ""


def compute_tp_target(entry_premium: float, dte: int) -> dict:
    """Returns dict with display string, target premium, and pct gain."""
    if entry_premium <= 0:
        return {"display": "Premium target N/A", "target_premium": None, "pct_gain": None, "entry_estimate": None}
    mult = tp_premium_multiplier(dte)
    target = round(entry_premium * mult, 2)
    pct = int((mult - 1) * 100)
    return {
        "display": f"${target:.2f}  (+{pct}% from ${entry_premium:.2f})",
        "target_premium": target,
        "pct_gain": pct,
        "entry_estimate": round(entry_premium, 2),
    }

def load_watches() -> list:
    if not os.path.exists(WATCHES_PATH): return []
    try:
        with open(WATCHES_PATH, 'r') as f: return json.load(f)
    except: return []

def save_watches(watches: list):
    atomic_write_json(WATCHES_PATH, watches)

def confirm_smc_retest(ticker: str, watch: dict) -> tuple:
    """Check if price has retested the FVG/OB zone with a rejection wick on intraday bars.
    Returns (confirmed: bool, reason: str)."""
    try:
        # Pick timeframe by DTE bucket (5m for 0-7DTE, 15m for 8-14DTE, 1h for 15+DTE)
        dte = watch.get('dte', 7)
        if dte <= 7:
            tf, lookback_hours = '5Min', 4
        elif dte <= 14:
            tf, lookback_hours = '15Min', 12
        else:
            tf, lookback_hours = '1Hour', 48

        end = datetime.utcnow()
        start = end - timedelta(hours=lookback_hours)
        df = fetch_alpaca_bars(ticker, tf, start.strftime('%Y-%m-%dT%H:%M:%SZ'), end.strftime('%Y-%m-%dT%H:%M:%SZ'))
        if df is None or df.empty: return (False, "no_intraday_data")

        zone_low = watch['zone_low']
        zone_high = watch['zone_high']
        ttype = watch['type']
        path = watch.get('path', 'PULLBACK')

        # Only inspect bars after the watch was created
        try:
            created = datetime.strptime(watch['created_at'], '%Y-%m-%dT%H:%M:%SZ')
            df = df[df.index >= created]
        except: pass
        if df.empty: return (False, "no_post_watch_bars")

        for _, bar in df.iterrows():
            high = float(bar['High']); low = float(bar['Low'])
            o = float(bar['Open']); c = float(bar['Close'])
            bar_range = high - low
            if bar_range <= 0: continue

            if path == 'PULLBACK':
                # Did this bar enter the FVG zone?
                entered = low <= zone_high and high >= zone_low
                if not entered: continue
                if ttype == 'CALL':
                    # Bullish rejection: lower wick > 40% of range, close > open
                    lower_wick = min(o, c) - low
                    if lower_wick / bar_range > 0.4 and c > o:
                        return (True, f"bull_rejection@{c:.2f}")
                else:
                    upper_wick = high - max(o, c)
                    if upper_wick / bar_range > 0.4 and c < o:
                        return (True, f"bear_rejection@{c:.2f}")
            else:  # BREAKOUT
                level = (zone_low + zone_high) / 2
                # Confirmation = close back above (CALL) / below (PUT) the level on a strong body
                if ttype == 'CALL':
                    body_strength = (c - o) / bar_range if bar_range > 0 else 0
                    if c > level and body_strength > 0.5:
                        return (True, f"breakout_confirm@{c:.2f}")
                else:
                    body_strength = (o - c) / bar_range if bar_range > 0 else 0
                    if c < level and body_strength > 0.5:
                        return (True, f"breakdown_confirm@{c:.2f}")
        return (False, "no_confirmation_yet")
    except Exception as e:
        return (False, f"err:{str(e)[:40]}")

def watch_age_hours(watch: dict) -> float:
    try:
        created = datetime.strptime(watch['created_at'], '%Y-%m-%dT%H:%M:%SZ')
        return (datetime.utcnow() - created).total_seconds() / 3600.0
    except: return 999

def watch_expired(watch: dict) -> bool:
    dte = watch.get('dte', 7)
    # Expiry windows scale with DTE: short-dated stale faster
    max_age_h = 1.0 if dte <= 7 else 4.0 if dte <= 14 else 12.0
    return watch_age_hours(watch) > max_age_h

def run_v4_meta_engine():
    print(f"[{datetime.utcnow().strftime('%H:%M:%SZ')}] 🧠 Booting V4.3 Watch-State Execution Engine...")
    regime = determine_market_regime()
    print(f"🌍 Live Macro Matrix: {regime}")

    watchlist = load_watchlist()
    if not watchlist: return
    signals = []

    # === STAGE 0: Process existing watches first ===
    existing_watches = load_watches()
    surviving_watches = []
    for w in existing_watches:
        if watch_expired(w):
            signals.append({
                "Ticker": w['ticker'], "Type": w['type'], "DTE": w.get('dte', 0),
                "Status": "WATCH_EXPIRED", "Confidence": f"{w.get('score', 0):.1f}",
                "Screener_Logic": w.get('screener_logic', 'Watch timed out before retest confirmation.')
            })
            continue
        confirmed, reason = confirm_smc_retest(w['ticker'], w)
        if confirmed:
            trig_status = f"TRIGGER_{w.get('path', 'PULLBACK')}_CONFIRMED"
            size_mult = min(1.0, max(0.25, (w.get('score', 40) - 40) / 60.0))
            signals.append({
                "Ticker": w['ticker'], "Type": w['type'], "DTE": w.get('dte', 0),
                "Status": trig_status, "Confidence": f"{w.get('score', 0):.1f}",
                "Entry_Reason": reason, "Size_Multiplier": round(size_mult, 2),
                "Exit_Protocol": w.get('exit_protocol', {}),
                # Surface contract details on the signal itself for the dashboard
                "Strike": w.get('strike'), "Expiration": w.get('expiry'),
                "Contract_Symbol": w.get('contract_symbol'),
                # Preserve the score_matrix from the watch — needed for quality_analysis
                "Score_Matrix": w.get('score_matrix', {}),
                "Screener_Logic": w.get('screener_logic', '')
            })
            print(f"🚀 {w['ticker']} -> {trig_status} | {reason} | size={size_mult:.2f}x")
        else:
            surviving_watches.append(w)
            watch_status = f"WATCH_{w.get('path', 'PULLBACK')}"
            signals.append({
                "Ticker": w['ticker'], "Type": w['type'], "DTE": w.get('dte', 0),
                "Status": watch_status, "Confidence": f"{w.get('score', 0):.1f}",
                "Exit_Protocol": w.get('exit_protocol', {}),
                "Score_Matrix": w.get('score_matrix', {}),
                "Strike": w.get('strike'), "Expiration": w.get('expiry'),
                "Contract_Symbol": w.get('contract_symbol'),
                "Screener_Logic": w.get('screener_logic', ''),
            })

    for ticker, info in watchlist.items():
        scr_logic = info.get("screener_logic", "Screener logic unavailable.")
        flow, early_status = extract_clustered_flow(ticker)

        if not flow:
            signals.append({ "Ticker": ticker, "Type": "UNKNOWN", "DTE": 0, "Status": early_status, "Confidence": "0.0", "Screener_Logic": scr_logic })
            continue

        dte = (datetime.strptime(flow['expiry'], "%Y-%m-%d") - datetime.now()).days if flow['expiry'] else 0
        base_score = 0

        tod_active = flow['tod_score']
        if dte >= 3 and tod_active < 0: tod_active = 0
        base_score += tod_active
        base_score -= flow['spread_penalty']
        
        prem = flow['total_premium']
        # Log-scale premium: smooth diminishing returns instead of step buckets
        # log10(100K)=5: 0pts, log10(1M)=6: ~7pts, log10(10M)=7: ~14pts, capped at 20
        prem_score = min(20, max(0, math.log10(max(prem / 100_000, 1)) * 7))
        # Persistence stacking bonus (only with real cluster size + repetition)
        if prem >= 500_000 and flow['sweep_count'] >= 3:
            prem_score = min(30, prem_score + 10)
        # Weak ask-dominance penalty (51% ask is not the same as 95% ask)
        ask_dom = flow.get('ask_dominance', 1.0)
        if ask_dom < 0.65:
            prem_score = max(0, prem_score - 8)
        base_score += prem_score

        if flow['confirmed_opening']: base_score += 10
        if regime == "CHOP": base_score -= 15
        # Symmetric trend bonus (CALL aligned with TREND_UP, PUT aligned with TREND_DOWN)
        if (flow['type'] == 'CALL' and regime == 'TREND_UP') or (flow['type'] == 'PUT' and regime == 'TREND_DOWN'):
            base_score += 10
        
        greek_pass, g_score = validate_greeks(ticker, flow['type'], flow['side'], regime)
        base_score += g_score 
            
        iv_pass, iv_penalty = validate_iv_rank(ticker, dte)
        if not iv_pass: 
            signals.append({ "Ticker": ticker, "Type": flow['type'], "DTE": dte, "Status": "REJECTED_IV_CRUSH", "Confidence": "0.0", "Screener_Logic": scr_logic })
            continue
            
        base_score -= iv_penalty
        dp_score = fetch_darkpool_confidence(ticker, flow['type'])
        base_score += dp_score
        
        # Dual-Execution Node Fork
        smc_score, is_pb, smc_sl, brk_score, is_brk, brk_sl, pb_zone, brk_level = evaluate_technical_structure(ticker, flow['type'], flow['spot'])

        final_pb_score = base_score + smc_score
        final_brk_score = base_score + brk_score

        # Don't double-watch a ticker that already has a live watch
        already_watched = any(w['ticker'] == ticker and w['type'] == flow['type'] for w in surviving_watches)

        status = "AWAITING_EXPANSION"
        final_selected_score = 0
        exit_protocol = {}
        size_mult = 0

        if not already_watched:
            now_str = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            # Watch expiry windows scale with DTE (must match watch_expired logic)
            max_age_h = 1.0 if dte <= 7 else 4.0 if dte <= 14 else 12.0
            expires_str = (datetime.utcnow() + timedelta(hours=max_age_h)).strftime('%Y-%m-%dT%H:%M:%SZ')

            # Contract info from the flow cluster (smart-money's actual bet)
            strike = flow.get('strike', 0)
            expiry = flow.get('expiry', '')
            contract_sym = build_occ_symbol(ticker, expiry, flow['type'], strike) if strike and expiry else ""

            if is_brk and final_brk_score >= 40 and brk_level is not None:
                # Create breakout watch (zone = ±0.3% around the level for retest tolerance)
                size_mult = min(0.5, max(0.15, (final_brk_score - 40) / 120.0))  # 0.15-0.5x for breakout (riskier)
                zone_low, zone_high = brk_level * 0.997, brk_level * 1.003
                tp = compute_tp_target(flow.get('mid_at_alert', 0), dte)
                exit_protocol = {
                    "TP": tp["display"], "TP_Premium_Target": tp["target_premium"],
                    "TP_Pct_Gain": tp["pct_gain"], "Entry_Premium_Estimate": tp["entry_estimate"],
                    "SL": f"{brk_sl:.2f}",
                    "TIME_STOP": f"{max(1, math.ceil(dte*0.4))} Days max",
                    "Size_Mult": round(size_mult, 2), "Path": "BREAKOUT",
                    "Watch_Zone_Low": round(zone_low, 2), "Watch_Zone_High": round(zone_high, 2),
                    "Created_UTC": now_str, "Expires_UTC": expires_str,
                    "Spot_At_Watch": round(flow['spot'], 2),
                    # Contract suggestion — what smart money was actually buying
                    "Strike": strike, "Expiration": expiry, "Contract_Symbol": contract_sym,
                }
                # Build score_matrix early so we can persist it on the watch dict
                breakout_metrics = { "persistence": round(prem_score, 1), "smc": smc_score, "dp": round(dp_score, 1), "greek": g_score, "iv": round(iv_penalty, 1), "open_conf": 10 if flow['confirmed_opening'] else 0, "tod": tod_active, "ask_dom": round(flow.get('ask_dominance', 0), 2) }
                surviving_watches.append({
                    "ticker": ticker, "type": flow['type'], "path": "BREAKOUT", "dte": dte,
                    "zone_low": zone_low, "zone_high": zone_high,
                    "score": final_brk_score, "created_at": now_str,
                    "strike": strike, "expiry": expiry, "contract_symbol": contract_sym,
                    "exit_protocol": exit_protocol, "screener_logic": scr_logic,
                    "score_matrix": breakout_metrics,  # persist for re-emit + trigger confirmation
                })
                status = "WATCH_BREAKOUT"
                final_selected_score = final_brk_score
            elif is_pb and final_pb_score >= 40 and pb_zone is not None:
                size_mult = min(1.0, max(0.25, (final_pb_score - 40) / 60.0))  # 0.25-1.0x for pullback
                tp = compute_tp_target(flow.get('mid_at_alert', 0), dte)
                exit_protocol = {
                    "TP": tp["display"], "TP_Premium_Target": tp["target_premium"],
                    "TP_Pct_Gain": tp["pct_gain"], "Entry_Premium_Estimate": tp["entry_estimate"],
                    "SL": f"{smc_sl:.2f}",
                    "TIME_STOP": f"{max(1, math.ceil(dte*0.4))} Days max",
                    "Size_Mult": round(size_mult, 2), "Path": "PULLBACK",
                    "Watch_Zone_Low": round(pb_zone[0], 2), "Watch_Zone_High": round(pb_zone[1], 2),
                    "Created_UTC": now_str, "Expires_UTC": expires_str,
                    "Spot_At_Watch": round(flow['spot'], 2),
                    "Strike": strike, "Expiration": expiry, "Contract_Symbol": contract_sym,
                }
                pullback_metrics = { "persistence": round(prem_score, 1), "smc": smc_score, "dp": round(dp_score, 1), "greek": g_score, "iv": round(iv_penalty, 1), "open_conf": 10 if flow['confirmed_opening'] else 0, "tod": tod_active, "ask_dom": round(flow.get('ask_dominance', 0), 2) }
                surviving_watches.append({
                    "ticker": ticker, "type": flow['type'], "path": "PULLBACK", "dte": dte,
                    "zone_low": pb_zone[0], "zone_high": pb_zone[1],
                    "score": final_pb_score, "created_at": now_str,
                    "strike": strike, "expiry": expiry, "contract_symbol": contract_sym,
                    "exit_protocol": exit_protocol, "screener_logic": scr_logic,
                    "score_matrix": pullback_metrics,
                })
                status = "WATCH_PULLBACK"
                final_selected_score = final_pb_score
            elif final_pb_score < 40 and final_brk_score < 40:
                status = "REJECTED_LOW_SCORE"
                final_selected_score = max(final_pb_score, final_brk_score)

        metrics = { "persistence": round(prem_score, 1), "smc": smc_score, "dp": round(dp_score, 1), "greek": g_score, "iv": round(iv_penalty, 1), "open_conf": 10 if flow['confirmed_opening'] else 0, "tod": tod_active, "ask_dom": round(flow.get('ask_dominance', 0), 2) }

        signals.append({
             "Ticker": ticker, "Type": flow['type'], "DTE": dte, "Status": status, "Confidence": f"{final_selected_score:.1f}",
             "Exit_Protocol": exit_protocol, "Score_Matrix": metrics, "Screener_Logic": scr_logic
        })
        if status.startswith("WATCH_"):
             print(f"👁️ {ticker} -> Score: {final_selected_score:.1f} | State: {status} | size_mult={size_mult:.2f}x | awaiting SMC retest")

    import pprint
    print("\n📈 FINAL V4.3 EXECUTION TARGETS:")
    for sig in signals:
        if sig['Status'].startswith("TRIGGER_") or sig['Status'].startswith("WATCH_"):
            pprint.pprint(sig, indent=2)

    out_path = os.path.join(os.path.dirname(__file__), 'v4_signals.json')
    now = datetime.utcnow()

    # Persist surviving watches for next pipeline cycle
    save_watches(surviving_watches)
    print(f"💾 Watches persisted: {len(surviving_watches)} active")

    ledger_path = os.path.join(os.path.dirname(__file__), 'v4_ledger.json')
    try:
        ledger = []
        if os.path.exists(ledger_path):
            with open(ledger_path, 'r') as f: ledger = json.load(f)

        today_str = now.strftime('%Y-%m-%d')
        for sig in signals:
            if sig['Status'] in ("AWAITING_EXPANSION",):
                continue
            # Dedup: same ticker+type+status+date counted once per day
            exists = any(t['Ticker'] == sig['Ticker'] and t['Type'] == sig['Type']
                         and t.get('Status', '') == sig['Status'] and t['Date'] == today_str for t in ledger)
            if not exists:
                entry = sig.copy()
                entry['Date'] = today_str; entry['Timestamp'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
                ledger.append(entry)
                if sig['Status'].startswith("TRIGGER_"):
                    print(f"📝 Memory Add: Logged New {sig['Status']} for {sig['Ticker']}")

        atomic_write_json(ledger_path, ledger)
    except Exception as e:
        print(f"⚠️ Ledger write failed: {e}")

    signals_for_ui = [s for s in signals if s['Status'].startswith("TRIGGER_")
                      or s['Status'].startswith("WATCH_") or s['Status'] == "AWAITING_EXPANSION"]

    # Backfill recently-confirmed triggers from the ledger (last 60 min) so a signal
    # doesn't vanish 5 min after firing. Without this, a KWEB-style same-cycle confirm
    # appears once and disappears before the user can see it.
    try:
        with open(ledger_path) as f:
            full_ledger = json.load(f)
        cutoff = now - timedelta(minutes=60)
        current_keys = {(s['Ticker'], s.get('Type', ''), s.get('Status', '')) for s in signals_for_ui}
        for entry in full_ledger:
            st = entry.get('Status', '')
            if not (st.startswith("TRIGGER_") and st.endswith("CONFIRMED")):
                continue
            try:
                entry_ts = datetime.strptime(entry.get('Timestamp', ''), '%Y-%m-%dT%H:%M:%SZ')
            except Exception:
                continue
            if entry_ts < cutoff:
                continue
            key = (entry.get('Ticker'), entry.get('Type', ''), st)
            if key in current_keys:
                continue
            enriched = dict(entry)
            enriched['_source'] = 'ledger_recent'
            enriched['_age_minutes'] = int((now - entry_ts).total_seconds() / 60)
            signals_for_ui.append(enriched)
    except Exception as e:
        print(f"⚠️ Recent-triggers backfill failed: {e}")

    next_run = (now + timedelta(seconds=SCAN_INTERVAL_SECONDS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {
        "metadata": {
            "last_updated": now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "next_run": next_run,
            "regime": regime,
            "active_watches": len(surviving_watches),
        },
        "signals": signals_for_ui,
    }
    atomic_write_json(out_path, payload)
        
if __name__ == "__main__":
    run_v4_meta_engine()
