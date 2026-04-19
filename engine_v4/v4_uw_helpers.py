"""
V4 UW HELPERS — shared wrappers for Unusual Whales endpoints.

One module so meta_engine, preposition_scanner, paper_trader, and armada
all hit the same battle-tested API call (timeout, try/except, normalization).

Endpoint groups:
  - oi_change(ticker)              → build vs unwind detection
  - earnings_within(ticker, days)  → True if earnings in next N days
  - economic_events_within(hours)  → list of high-impact macro events
  - darkpool_recent(ticker, mins)  → recent block trades
  - short_squeeze_metrics(ticker)  → borrow fee, availability, SI/float, FTDs
  - max_pain(ticker, expiry)       → max pain strike + neighbors
  - spot_exposures_strike(ticker)  → per-strike gamma walls
  - seasonality(ticker, month)     → historical positive_months_perc
  - news_recent(ticker, hours)     → recent headlines + sentiment
  - sector_etf_strength()          → all 11 sector ETFs in one call
  - options_volume(ticker)         → daily call/put balance + ask-side dom
  - interpolated_iv(ticker)        → IV term structure
  - intraday_options_volume(ticker)→ today's call/put + premium

All functions return None or a sentinel on failure — callers must handle.
Never raises. ~10s timeout per call.
"""
import os
import requests
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))
except Exception:
    pass

UW_KEY = os.getenv("UW_API_KEY", "").replace('"', '')
HEADERS = {"Authorization": f"Bearer {UW_KEY}", "Accept": "application/json"}
BASE = "https://api.unusualwhales.com"
TIMEOUT = 10
UTC = timezone.utc


_LOG_NON_200 = os.getenv("UW_HELPERS_DEBUG", "").lower() in ("1", "true", "yes")

def _get(path: str, params: dict = None) -> dict:
    """Returns parsed JSON or None. Never raises.
    Logs failure type (timeout vs 5xx vs JSON parse) when UW_HELPERS_DEBUG is set,
    so ops can distinguish 'API down' from 'no data' without re-running."""
    try:
        r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params or {}, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        if _LOG_NON_200:
            print(f"[uw_helpers] {path} HTTP {r.status_code}: {r.text[:120]}")
    except requests.Timeout:
        if _LOG_NON_200: print(f"[uw_helpers] {path} TIMEOUT after {TIMEOUT}s")
    except requests.RequestException as e:
        if _LOG_NON_200: print(f"[uw_helpers] {path} {type(e).__name__}: {str(e)[:120]}")
    except ValueError as e:
        if _LOG_NON_200: print(f"[uw_helpers] {path} JSON parse error: {str(e)[:120]}")
    except Exception as e:
        # Catch-all so callers never crash, but always log so we can see anomalies
        print(f"[uw_helpers] {path} UNEXPECTED {type(e).__name__}: {str(e)[:120]}")
    return None


# ---------------- INTRADAY ----------------

def oi_change(ticker: str) -> list:
    """Today's per-contract OI change. Returns list of dicts with:
    option_symbol, volume, curr_oi, prev_ask_volume, prev_bid_volume, prev_mid_volume.
    Build = curr_oi rises (institutions positioning). Unwind = curr_oi falls (closing)."""
    j = _get(f"/api/stock/{ticker}/oi-change")
    if not j: return []
    return j.get('data', []) or []


def is_unwind(oi_records: list, contract_symbol: str) -> bool:
    """True if the contract appears in oi_records with shrinking OI.
    Used to filter out 'bullish-looking' flow that's actually closing trades."""
    for rec in oi_records:
        if rec.get('option_symbol') == contract_symbol:
            try:
                curr = int(rec.get('curr_oi', 0))
                # ask_volume - bid_volume tells direction; if mostly bid_volume + curr_oi shrinks → close
                prev_ask = int(rec.get('prev_ask_volume', 0))
                prev_bid = int(rec.get('prev_bid_volume', 0))
                # Sell-side dominant volume → likely closing
                if prev_bid > prev_ask * 1.5 and curr < (prev_ask + prev_bid):
                    return True
            except (TypeError, ValueError):
                pass
    return False


def darkpool_recent(ticker: str, minutes: int = 60, min_premium: float = 100_000) -> list:
    """Block trades for ticker in the last N minutes above min_premium.
    Returns list of {price, size, premium, executed_at, nbbo_bid, nbbo_ask}."""
    j = _get(f"/api/darkpool/{ticker}", {"limit": 200})
    if not j: return []
    records = j.get('data', []) or []
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    out = []
    for r in records:
        try:
            ts_str = r.get('executed_at', '')
            if not ts_str: continue
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            prem = float(r.get('premium', 0))
            if ts >= cutoff and prem >= min_premium and not r.get('canceled'):
                out.append(r)
        except Exception:
            continue
    return out


def darkpool_score(ticker: str, current_price: float, direction: str) -> tuple:
    """Returns (bonus_pts, summary_str). Looks at last 60min DP prints near current price.
    +10 if >$5M total in directionally-aligned prints (above mid for CALL, below for PUT)."""
    if not current_price or current_price <= 0:
        return (0, "no current price for DP comparison")
    prints = darkpool_recent(ticker, minutes=60, min_premium=100_000)
    if not prints: return (0, "no recent DP prints")
    aligned = []
    for p in prints:
        try:
            price = float(p.get('price', 0))
            prem = float(p.get('premium', 0))
            if direction == 'CALL' and price >= current_price * 0.99:  # accumulating at/above price
                aligned.append(prem)
            elif direction == 'PUT' and price <= current_price * 1.01:
                aligned.append(prem)
        except Exception:
            continue
    total = sum(aligned)
    if total >= 5_000_000:
        return (10, f"DP confirms {direction}: ${total/1e6:.1f}M aligned prints last 60min")
    if total >= 1_000_000:
        return (5, f"DP modest {direction} confirm: ${total/1e6:.1f}M")
    return (0, f"DP weak: ${total/1e6:.1f}M aligned")


# ---------------- SQUEEZE ----------------

_short_cache = {}

def short_squeeze_metrics(ticker: str) -> dict:
    """Returns {borrow_fee_pct, shares_available, si_pct_float, days_to_cover, ftds_recent}.
    Cached per-process per-day to avoid repeat calls."""
    today = datetime.now(UTC).date().isoformat()
    if (ticker, today) in _short_cache:
        return _short_cache[(ticker, today)]

    out = {'borrow_fee_pct': None, 'shares_available': None,
           'si_pct_float': None, 'days_to_cover': None, 'ftds_recent': 0}

    # Latest borrow data
    d = _get(f"/api/shorts/{ticker}/data")
    if d:
        recs = d.get('data', []) or []
        if recs:
            latest = recs[0]
            try:
                out['borrow_fee_pct'] = float(latest.get('fee_rate', 0))
                out['shares_available'] = int(latest.get('short_shares_available', 0) or 0)
            except Exception: pass

    # SI vs float
    d = _get(f"/api/shorts/{ticker}/interest-float")
    if d:
        recs = d.get('data', []) or []
        if recs:
            latest = recs[0]
            try:
                out['si_pct_float'] = float(latest.get('percent_returned', 0))
                out['days_to_cover'] = float(latest.get('days_to_cover_returned', 0))
            except Exception: pass

    # FTDs in last 30 days
    d = _get(f"/api/shorts/{ticker}/ftds")
    if d:
        recs = d.get('data', []) or []
        cutoff = datetime.now(UTC).date() - timedelta(days=30)
        recent = [r for r in recs if r.get('date', '') and r['date'] >= cutoff.isoformat()]
        out['ftds_recent'] = len(recent)

    _short_cache[(ticker, today)] = out
    return out


def squeeze_score(ticker: str, direction: str) -> tuple:
    """Returns (bonus_pts, summary). Squeeze CALLs get bonus, PUTs get penalty
    when short interest is high (squeeze risk against PUT thesis)."""
    if direction not in ('CALL', 'PUT'): return (0, "no direction")
    m = short_squeeze_metrics(ticker)
    fee = m.get('borrow_fee_pct') or 0
    si = m.get('si_pct_float') or 0
    ftds = m.get('ftds_recent') or 0

    # Squeeze score 0-25
    score = 0
    parts = []
    if fee >= 10:    score += 10; parts.append(f"borrow fee {fee:.1f}%")
    elif fee >= 5:   score += 5;  parts.append(f"borrow fee {fee:.1f}%")
    if si >= 20:     score += 10; parts.append(f"SI {si:.0f}% float")
    elif si >= 10:   score += 5;  parts.append(f"SI {si:.0f}% float")
    if ftds >= 10:   score += 5;  parts.append(f"{ftds} FTDs/30d")

    if score == 0: return (0, "no squeeze")
    summary = f"squeeze setup: {', '.join(parts)}"
    # CALLs benefit, PUTs hurt (fighting the squeeze)
    return (score if direction == 'CALL' else -score, summary)


# ---------------- CALENDAR / CATALYST ----------------

_earnings_cache = {}

def earnings_within(ticker: str, days: int = 5) -> dict:
    """Returns {within: bool, days_until: int|None, report_date: str|None, report_time: str|None}.
    Cached per-process per-day."""
    today = datetime.now(UTC).date()
    cache_key = (ticker, today.isoformat())
    if cache_key in _earnings_cache:
        return _earnings_cache[cache_key]

    out = {'within': False, 'days_until': None, 'report_date': None, 'report_time': None}
    d = _get(f"/api/stock/{ticker}/earnings")
    if not d:
        _earnings_cache[cache_key] = out
        return out

    recs = d.get('data', []) or []
    # Find next future earnings
    future = []
    for r in recs:
        rd_str = r.get('report_date', '')
        if not rd_str: continue
        try:
            rd = datetime.strptime(rd_str, '%Y-%m-%d').date()
            if rd >= today:
                future.append((rd, r))
        except Exception:
            continue
    if future:
        future.sort(key=lambda x: x[0])
        rd, rec = future[0]
        d_until = (rd - today).days
        out = {
            'within': d_until <= days,
            'days_until': d_until,
            'report_date': rd.isoformat(),
            'report_time': rec.get('report_time'),
        }
    _earnings_cache[cache_key] = out
    return out


_econ_cache = {'fetched_at': None, 'data': []}

def economic_events_within(hours: int = 24) -> list:
    """Returns list of high-impact macro events in next N hours (UTC).
    Cached for 6 hours within process (calendar doesn't change intraday)."""
    now = datetime.now(UTC)
    if _econ_cache['fetched_at'] and (now - _econ_cache['fetched_at']).total_seconds() < 21600:
        cached = _econ_cache['data']
    else:
        d = _get("/api/market/economic-calendar")
        cached = (d or {}).get('data', []) or []
        _econ_cache['fetched_at'] = now
        _econ_cache['data'] = cached

    cutoff = now + timedelta(hours=hours)
    out = []
    # Keywords for events that move markets meaningfully
    high_impact_kw = ['CPI', 'inflation', 'FOMC', 'Fed', 'rate decision',
                      'nonfarm', 'payroll', 'employment', 'GDP', 'PPI',
                      'PCE', 'jobless claims', 'unemployment', 'manufacturing PMI']
    for ev in cached:
        try:
            ev_time_str = ev.get('time', '')
            if not ev_time_str: continue
            ev_time = datetime.fromisoformat(ev_time_str.replace('Z', '+00:00'))
            if not (now <= ev_time <= cutoff): continue
            event_desc = (ev.get('event', '') or '').lower()
            if any(kw.lower() in event_desc for kw in high_impact_kw):
                out.append({
                    'event': ev.get('event'),
                    'time': ev_time_str,
                    'hours_until': (ev_time - now).total_seconds() / 3600,
                })
        except Exception:
            continue
    return out


# ---------------- TRADE-LEVEL HELPERS (paper_trader) ----------------

def max_pain(ticker: str, expiry_yyyy_mm_dd: str) -> dict:
    """Returns {max_pain, next_upper, next_lower, close} for the given expiry, or {}."""
    d = _get(f"/api/stock/{ticker}/max-pain")
    if not d: return {}
    for rec in d.get('data', []) or []:
        if rec.get('expiry') == expiry_yyyy_mm_dd:
            try:
                return {
                    'max_pain': float(rec.get('max_pain', 0)),
                    'next_upper': float(rec.get('next_upper_strike', 0)),
                    'next_lower': float(rec.get('next_lower_strike', 0)),
                    'close': float(rec.get('close', 0)),
                }
            except Exception: pass
    return {}


def spot_exposures_strike(ticker: str) -> list:
    """Returns per-strike gamma exposure for today.
    List of {strike, call_gamma_oi, put_gamma_oi, call_charm_oi, put_charm_oi, ...}."""
    d = _get(f"/api/stock/{ticker}/spot-exposures/strike")
    if not d: return []
    return d.get('data', []) or []


def gamma_walls(ticker: str, current_price: float) -> dict:
    """Find the largest gamma wall above and below current_price.
    Returns {upper_wall: {strike, magnitude}, lower_wall: {strike, magnitude}}."""
    strikes = spot_exposures_strike(ticker)
    if not strikes: return {}
    upper = None; lower = None
    for r in strikes:
        try:
            strike = float(r.get('strike', 0))
            # Net gamma OI (call + put magnitudes; calls add positive, puts add negative)
            mag = abs(float(r.get('call_gamma_oi', 0))) + abs(float(r.get('put_gamma_oi', 0)))
            if strike > current_price:
                if not upper or mag > upper['magnitude']:
                    upper = {'strike': strike, 'magnitude': mag}
            elif strike < current_price:
                if not lower or mag > lower['magnitude']:
                    lower = {'strike': strike, 'magnitude': mag}
        except Exception:
            continue
    return {'upper_wall': upper, 'lower_wall': lower}


# ---------------- PREPOSITION REFINEMENTS ----------------

def seasonality_score(ticker: str, month: int) -> tuple:
    """Returns (bonus_pts, summary). Penalize trades in seasonally weak months.
    +5 if positive_months_perc >= 70%, -5 if <= 30%."""
    d = _get(f"/api/seasonality/{ticker}/monthly")
    if not d: return (0, "no seasonality data")
    recs = d.get('data', []) or []
    target = next((r for r in recs if int(r.get('month', 0)) == month), None)
    if not target: return (0, "no data for current month")
    try:
        pct = float(target.get('positive_months_perc', 50))
        if pct >= 70:    return (5, f"month seasonally bullish ({pct:.0f}% positive)")
        elif pct <= 30:  return (-5, f"month seasonally bearish ({pct:.0f}% positive)")
        else:            return (0, f"month neutral ({pct:.0f}% positive)")
    except Exception:
        return (0, "parse error")


_sector_cache = {'fetched_at': None, 'data': {}}

def sector_etf_strength() -> dict:
    """Returns dict of all sector ETFs with day's pct_change and put/call ratio.
    Cached per-process for 10 min."""
    now = datetime.now(UTC)
    if _sector_cache['fetched_at'] and (now - _sector_cache['fetched_at']).total_seconds() < 600:
        return _sector_cache['data']

    d = _get("/api/market/sector-etfs")
    if not d: return {}
    out = {}
    for r in (d.get('data', []) or []):
        try:
            t = r.get('ticker', '')
            if not t: continue
            o = float(r.get('open', 0)); l = float(r.get('last', 0))
            cv = float(r.get('call_volume', 0)); pv = float(r.get('put_volume', 0))
            out[t] = {
                'name': r.get('full_name'),
                'pct_change': ((l - o) / o * 100) if o else 0,
                'put_call_ratio': (pv / cv) if cv else 0,
                'last': l,
            }
        except Exception:
            continue
    _sector_cache['fetched_at'] = now
    _sector_cache['data'] = out
    return out


# ---------------- META REFINEMENTS ----------------

def options_volume_today(ticker: str) -> dict:
    """Returns {call_pct_ask, put_pct_ask, net_call_premium, net_put_premium, cp_ratio}.
    Direction confirmation: high call_pct_ask + cp_ratio<1 means bullish day."""
    d = _get(f"/api/stock/{ticker}/options-volume")
    if not d: return {}
    recs = d.get('data', []) or []
    if not recs: return {}
    r = recs[0]
    try:
        cv = float(r.get('call_volume', 0))
        pv = float(r.get('put_volume', 0))
        cv_ask = float(r.get('call_volume_ask_side', 0))
        pv_ask = float(r.get('put_volume_ask_side', 0))
        return {
            'call_volume': cv, 'put_volume': pv,
            'call_pct_ask': (cv_ask / cv) if cv else 0,
            'put_pct_ask': (pv_ask / pv) if pv else 0,
            'net_call_premium': float(r.get('net_call_premium', 0)),
            'net_put_premium': float(r.get('net_put_premium', 0)),
            'cp_ratio': (pv / cv) if cv else 0,
        }
    except Exception:
        return {}


def interpolated_iv_term(ticker: str) -> list:
    """Returns IV term structure as list of {days, volatility, percentile, implied_move_perc}.
    Detect inversions: short-DTE IV > long-DTE IV → event risk priced in."""
    d = _get(f"/api/stock/{ticker}/interpolated-iv")
    if not d: return []
    out = []
    for r in (d.get('data', []) or []):
        try:
            out.append({
                'days': int(r.get('days', 0)),
                'volatility': float(r.get('volatility', 0)),
                'percentile': float(r.get('percentile', 0)),
                'implied_move_pct': float(r.get('implied_move_perc', 0)),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x['days'])
    return out


def iv_term_inversion(ticker: str) -> tuple:
    """Returns (is_inverted, summary). Front-week IV >> longer IV = event risk → IV crush imminent.
    Tuned: requires 2.0x median-to-median ratio (true event-risk pricing), not 1.3x peak-to-trough.
    Original 1.3x was too sensitive — normal day-to-day IV variation easily clears it."""
    term = interpolated_iv_term(ticker)
    if len(term) < 3: return (False, "insufficient term-structure data")
    short = [r['volatility'] for r in term if r['days'] <= 7]
    medium = [r['volatility'] for r in term if 14 <= r['days'] <= 30]
    if not (short and medium): return (False, "no comparable buckets")
    # Median vs median is more stable than max/min — earnings IV is usually 2-5x medium
    import statistics as _stats
    short_iv = _stats.median(short)
    med_iv = _stats.median(medium)
    if short_iv > med_iv * 2.0:  # require 2x to call it event risk
        return (True, f"IV term inverted: short {short_iv:.2f} > 2x medium {med_iv:.2f} → event risk")
    return (False, f"IV term normal: short {short_iv:.2f} vs medium {med_iv:.2f}")


# ---------------- NEWS ----------------

def news_recent(ticker: str = None, hours: int = 2, limit: int = 50) -> list:
    """Returns recent news headlines (optionally filtered by ticker presence in tags).
    Each item: {headline, source, sentiment, tickers, created_at, is_major}."""
    d = _get("/api/news/headlines", {"limit": limit})
    if not d: return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    out = []
    for r in (d.get('data', []) or []):
        try:
            ts_str = r.get('created_at', '')
            if not ts_str: continue
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            if ts < cutoff: continue
            if ticker:
                tickers = r.get('tickers', []) or []
                if ticker not in tickers: continue
            out.append(r)
        except Exception:
            continue
    return out


def has_recent_catalyst(ticker: str, hours: int = 2) -> tuple:
    """Returns (bool, summary). True if there's a major news event for this ticker."""
    news = news_recent(ticker, hours=hours)
    if not news: return (False, "no recent news")
    major = [n for n in news if n.get('is_major')]
    if major:
        return (True, f"{len(major)} major headline(s) in last {hours}h: {major[0].get('headline','')[:80]}")
    return (False, f"{len(news)} non-major headline(s)")


if __name__ == "__main__":
    # Quick smoke test
    print("=== UW HELPERS smoke test ===")
    print("oi_change(NVDA):", len(oi_change("NVDA")), "contracts")
    print("earnings_within(NVDA, 30):", earnings_within("NVDA", 30))
    print("economic_events_within(48):", len(economic_events_within(48)), "events")
    print("squeeze_score(GME, CALL):", squeeze_score("GME", "CALL"))
    print("max_pain(SPY, 2026-04-25):", max_pain("SPY", "2026-04-25"))
    print("seasonality_score(NVDA, 4):", seasonality_score("NVDA", 4))
    print("sector_etf_strength count:", len(sector_etf_strength()))
    print("options_volume_today(NVDA):", options_volume_today("NVDA"))
    print("iv_term_inversion(NVDA):", iv_term_inversion("NVDA"))
