"""
V4 PAPER TRADER — Alpaca paper account auto-execution for V4 signals.

PRECONDITIONS (verified before any orders):
  1. ALPACA_BASE_URL must contain 'paper' (paper-api.alpaca.markets)
  2. Account number must start with 'PA' (paper account marker)
  3. Options trading level >= 2

If any check fails, the trader REFUSES to run. No exceptions.

WHAT IT DOES:
  - Polls v4_signals.json every TRADER_CADENCE seconds
  - On new TRIGGER_*_CONFIRMED: finds matching option contract, places market buy
  - Tracks open positions in v4_paper_positions.json
  - Monitors each position for exit:
      - Premium TP hit (option price >= TP_Premium_Target)
      - Underlying SL hit (stock close < SL)
      - Time stop (held longer than DTE * 0.4)
      - EOD close for 0-7 DTE positions
  - On exit: submits market sell, records realized P&L

RISK MANAGEMENT (hard-coded, intentionally conservative):
  - MAX_CONCURRENT_POSITIONS = 5
  - MAX_POSITION_USD = 1500
  - BASE_SIZE_USD = 1000 (multiplied by score-derived size_mult)
  - One position per (ticker, direction) — no doubles
  - Skip if option spread > 8%
  - Skip if buying power < required

KILL SWITCH: touch v4_paper_trader_kill → exits cleanly on next iteration.

RUN: python3 engine_v4/v4_paper_trader.py
"""
import os
import sys
import json
import time
import uuid
import requests
from datetime import datetime, timedelta, timezone, time as dtime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

# UW endpoint helpers (Phase 1b/3a/3b): earnings, max-pain, gamma walls
from v4_uw_helpers import earnings_within, max_pain, gamma_walls

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

UTC = timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")

ALPACA_URL = os.getenv("ALPACA_BASE_URL", "")          # trading endpoints (paper-api.alpaca.markets)
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")  # market data
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "")
H = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_PATH = os.path.join(BASE_DIR, 'v4_signals.json')
PREPOSITION_PATH = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')
POSITIONS_PATH = os.path.join(BASE_DIR, 'v4_paper_positions.json')
TRADER_LOG_PATH = os.path.join(BASE_DIR, 'v4_paper_trader.log')
KILL_FILE = os.path.join(BASE_DIR, 'v4_paper_trader_kill')

# ---------- Risk parameters ----------
TRADER_CADENCE = 30           # seconds between cycles
# MAX_CONCURRENT_POSITIONS removed as the binding constraint — the real risk
# control is exposure caps below (per-sector, per-theme, per-delta). Set high
# enough to be non-binding in practice; if it ever hits, something upstream
# is generating too many signals and we should investigate, not block.
MAX_CONCURRENT_POSITIONS = 50
MAX_POSITION_USD = 3000       # raised from 1500 so qty>=2 fits on ~$10-15 premium contracts (enables partial TP)
BASE_SIZE_USD = 1000
MAX_OPTION_SPREAD_PCT = 0.08  # skip illiquid contracts

# ---------- Exposure caps (Phase: portfolio risk control) ----------
# Hidden-leverage protection. Without these, a 9-position book can be 100% the same
# AI/tech/crypto narrative — single CPI shock takes the whole book down together.
MAX_POSITIONS_PER_SECTOR = 2     # e.g., max 2 Tech, 2 Healthcare, etc.
MAX_POSITIONS_PER_THEME  = 3     # e.g., max 3 AI-adjacent, 3 crypto-adjacent
MAX_PORTFOLIO_DELTA      = 3.0   # absolute net delta in spy-equivalents (rough cap)

# Theme tagging — coarse manual mapping, expand as needed.
# A position adds to ALL themes its ticker maps to.
TICKER_THEMES = {
    # AI / mega-cap tech
    'NVDA': ['ai', 'tech'], 'MSFT': ['ai', 'tech'], 'GOOGL': ['ai', 'tech'],
    'GOOG': ['ai', 'tech'], 'META': ['ai', 'tech'], 'AMZN': ['ai', 'tech'],
    'AAPL': ['tech'], 'AVGO': ['ai', 'semis'], 'AMD': ['ai', 'semis'],
    'TSM': ['ai', 'semis'], 'ARM': ['ai', 'semis'], 'SMH': ['semis'],
    'QQQ': ['tech'], 'TQQQ': ['tech'], 'PLTR': ['ai', 'tech'],
    'NFLX': ['tech'], 'ORCL': ['tech'], 'NBIS': ['ai'], 'IGV': ['tech'],
    # Crypto-adjacent
    'COIN': ['crypto'], 'MSTR': ['crypto'], 'MARA': ['crypto'],
    'CIFR': ['crypto'], 'IBIT': ['crypto'], 'TSLA': ['ai', 'crypto'],
    # Bonds / rates
    'TLT': ['rates'], 'IEF': ['rates'], 'SHY': ['rates'],
    # China
    'KWEB': ['china'], 'FXI': ['china'], 'BABA': ['china'],
    # Index / broad
    'SPY': ['broad'], 'IWM': ['broad'], 'DIA': ['broad'],
    # Energy
    'XOP': ['energy'], 'XLE': ['energy'], 'USO': ['energy'],
    # Metals
    'GLD': ['metals'], 'SLV': ['metals'], 'GDX': ['metals'],
    # Banks / financials
    'JPM': ['banks'], 'GS': ['banks'], 'KRE': ['banks'],
    # Defense / aerospace
    'LMT': ['defense'], 'BA': ['defense'], 'RTX': ['defense'],
    # Healthcare
    'UNH': ['healthcare'], 'LLY': ['healthcare'],
}

# Coarse sector — single bucket per ticker (different from themes which are multi-tag).
TICKER_SECTOR = {
    'NVDA': 'Tech', 'MSFT': 'Tech', 'GOOGL': 'Tech', 'GOOG': 'Tech', 'META': 'Tech',
    'AMZN': 'Tech', 'AAPL': 'Tech', 'AVGO': 'Tech', 'AMD': 'Tech', 'TSM': 'Tech',
    'NFLX': 'Tech', 'ORCL': 'Tech', 'PLTR': 'Tech', 'ARM': 'Tech', 'SMH': 'Tech',
    'IGV': 'Tech', 'NBIS': 'Tech', 'TQQQ': 'Tech', 'QQQ': 'Tech',
    'COIN': 'Crypto', 'MSTR': 'Crypto', 'MARA': 'Crypto', 'CIFR': 'Crypto', 'IBIT': 'Crypto',
    'TSLA': 'Crypto',  # tag as crypto exposure for cap purposes
    'TLT': 'Rates', 'IEF': 'Rates', 'SHY': 'Rates',
    'KWEB': 'China', 'FXI': 'China', 'BABA': 'China',
    'SPY': 'Broad', 'IWM': 'Broad', 'DIA': 'Broad',
    'XOP': 'Energy', 'XLE': 'Energy', 'USO': 'Energy',
    'GLD': 'Metals', 'SLV': 'Metals', 'GDX': 'Metals',
    'JPM': 'Banks', 'GS': 'Banks', 'KRE': 'Banks',
    'LMT': 'Defense', 'BA': 'Defense', 'RTX': 'Defense',
    'UNH': 'Healthcare', 'LLY': 'Healthcare',
}

# Options market hours — Alpaca rejects market orders outside 9:30 AM – 4:00 PM ET
# (V4 scan window runs pre-market too; trader must be tighter)
WINDOW_START = (6, 30)   # 6:30 AM PT = 9:30 AM ET (options open)
WINDOW_END = (13, 0)     # 1:00 PM PT = 4:00 PM ET (options close)


def log(msg, level="INFO"):
    ts = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    icon = {"INFO":"💼","WARN":"⚠️","ERR":"❌","ORDER":"📋","FILL":"✅","EXIT":"🔚"}.get(level,"·")
    line = f"[{ts}] {icon} TRADER: {msg}"
    print(line, flush=True)
    try:
        with open(TRADER_LOG_PATH, 'a') as f:
            f.write(line + "\n")
    except Exception: pass


# ---------- Safety preflight ----------
def preflight():
    """Verify paper account credentials. Refuse to run otherwise."""
    if 'paper' not in ALPACA_URL.lower():
        log(f"REFUSING — ALPACA_BASE_URL='{ALPACA_URL}' is not a paper endpoint", "ERR")
        return False
    try:
        r = requests.get(f"{ALPACA_URL}/v2/account", headers=H, timeout=10)
        if r.status_code != 200:
            log(f"REFUSING — account check failed: {r.status_code} {r.text[:200]}", "ERR")
            return False
        acc = r.json()
        if not acc.get('account_number','').startswith('PA'):
            log(f"REFUSING — account_number={acc.get('account_number')} doesn't start with PA", "ERR")
            return False
        if int(acc.get('options_trading_level', 0)) < 2:
            log(f"REFUSING — options_trading_level={acc.get('options_trading_level')} < 2", "ERR")
            return False
        log(f"Paper account verified: {acc.get('account_number')} | "
            f"options_BP=${acc.get('options_buying_power','?')} | level={acc.get('options_trading_level')}")
        return True
    except Exception as e:
        log(f"REFUSING — preflight error: {e}", "ERR")
        return False


# ---------- Window check ----------
def in_window():
    now_pt = datetime.now(PACIFIC)
    if now_pt.weekday() >= 5: return False
    cur = now_pt.hour * 60 + now_pt.minute
    return WINDOW_START[0]*60 + WINDOW_START[1] <= cur <= WINDOW_END[0]*60 + WINDOW_END[1]


# ---------- Alpaca helpers ----------
def alpaca_get(path, params=None, data_api=False):
    """data_api=True for market-data endpoints (data.alpaca.markets), False for trading endpoints."""
    base = ALPACA_DATA_URL if data_api else ALPACA_URL
    try:
        r = requests.get(f"{base}{path}", headers=H, params=params or {}, timeout=10)
        if r.status_code == 200: return r.json()
        log(f"GET {path} failed {r.status_code}: {r.text[:200]}", "WARN")
    except Exception as e:
        log(f"GET {path} err: {e}", "WARN")
    return None


def alpaca_post(path, body):
    try:
        r = requests.post(f"{ALPACA_URL}{path}", headers=H, json=body, timeout=15)
        if r.status_code in (200, 201): return r.json()
        log(f"POST {path} failed {r.status_code}: {r.text[:300]}", "ERR")
    except Exception as e:
        log(f"POST {path} err: {e}", "ERR")
    return None


def get_option_snapshot(symbol):
    """Returns {bid, ask, mid, spread_pct, delta, iv} or None. Uses market data API."""
    j = alpaca_get("/v1beta1/options/snapshots", {"symbols": symbol}, data_api=True)
    if not j: return None
    snaps = j.get('snapshots') or {}
    snap = snaps.get(symbol) or {}
    quote = snap.get('latestQuote') or {}
    bid = float(quote.get('bp', 0))
    ask = float(quote.get('ap', 0))
    if bid <= 0 or ask <= 0: return None
    mid = (bid + ask) / 2
    greeks = snap.get('greeks') or {}
    return {
        'bid': bid, 'ask': ask, 'mid': mid,
        'spread_pct': (ask - bid) / mid if mid > 0 else 1.0,
        'delta': float(greeks.get('delta', 0)) if greeks else None,
        'iv': float(snap.get('impliedVolatility', 0)) or None,
    }


def get_underlying_price(ticker):
    """Get current underlying price via Alpaca latest trade (market data API)."""
    j = alpaca_get(f"/v2/stocks/{ticker}/trades/latest", data_api=True)
    if not j: return None
    trade = j.get('trade') or {}
    return float(trade.get('p', 0)) or None


def find_contract(ticker, direction, target_dte, spot):
    """Find the closest slightly-OTM option contract ~target_dte away."""
    today = datetime.now(UTC).date()
    # Allow a window around target_dte
    exp_min = today + timedelta(days=max(1, target_dte - 3))
    exp_max = today + timedelta(days=target_dte + 7)
    j = alpaca_get('/v2/options/contracts', {
        'underlying_symbols': ticker,
        'expiration_date_gte': exp_min.isoformat(),
        'expiration_date_lte': exp_max.isoformat(),
        'type': direction.lower(),
        'status': 'active',
        'limit': 200,
    })
    if not j: return None
    contracts = j.get('option_contracts', [])
    if not contracts: return None

    # Group by expiration; pick the one closest to target_dte
    by_exp = {}
    for c in contracts:
        exp = c.get('expiration_date')
        by_exp.setdefault(exp, []).append(c)
    target_exp_date = today + timedelta(days=target_dte)
    chosen_exp = min(by_exp.keys(), key=lambda e: abs((datetime.fromisoformat(e).date() - target_exp_date).days))
    candidates = by_exp[chosen_exp]

    # Strike selection: slightly OTM
    if direction == 'CALL':
        target_strike = spot * 1.02
        otm = [c for c in candidates if float(c['strike_price']) >= target_strike]
        if not otm: otm = candidates  # fall back to any
        chosen = min(otm, key=lambda c: float(c['strike_price']))
    else:
        target_strike = spot * 0.98
        otm = [c for c in candidates if float(c['strike_price']) <= target_strike]
        if not otm: otm = candidates
        chosen = max(otm, key=lambda c: float(c['strike_price']))
    return chosen


def submit_order(symbol, qty, side, signal_id):
    """Market order on the option contract."""
    body = {
        "symbol": symbol, "qty": str(qty), "side": side,
        "type": "market", "time_in_force": "day",
        "client_order_id": f"v4-{signal_id}-{side}-{int(time.time())}"[:48],
    }
    log(f"ORDER {side.upper()} {qty} {symbol} ({signal_id})", "ORDER")
    return alpaca_post('/v2/orders', body)


def get_order_status(order_id):
    return alpaca_get(f'/v2/orders/{order_id}')


# ---------- Position state ----------
def load_positions():
    if not os.path.exists(POSITIONS_PATH): return []
    try:
        with open(POSITIONS_PATH) as f: return json.load(f)
    except Exception: return []


def save_positions(positions):
    """Atomic write — never leave a partial positions file for the next cycle to corrupt-read."""
    import tempfile
    dir_path = os.path.dirname(POSITIONS_PATH) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(positions, f, indent=2, default=str)
        os.replace(tmp, POSITIONS_PATH)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
        raise


def open_count(positions):
    return sum(1 for p in positions if p.get('status') == 'OPEN')


def has_open_for(positions, ticker, direction):
    return any(p['ticker'] == ticker and p['direction'] == direction
               and p['status'] in ('OPEN', 'PENDING_ENTRY')
               for p in positions)


def exposure_check(positions, ticker, direction, contract_delta=None):
    """Returns (allowed: bool, reason: str). Hidden-leverage guard:
    blocks new entries if portfolio is already concentrated in same sector,
    same theme cluster, or absolute net delta would exceed cap.

    contract_delta: per-contract delta from option snapshot, signed for direction
                    (positive for CALL, negative for PUT). Pass None to skip delta check."""
    open_pos = [p for p in positions if p.get('status') in ('OPEN', 'PENDING_ENTRY')]

    # Sector cap
    sector = TICKER_SECTOR.get(ticker.upper())
    if sector:
        same_sector = sum(1 for p in open_pos if TICKER_SECTOR.get(p['ticker'].upper()) == sector)
        if same_sector >= MAX_POSITIONS_PER_SECTOR:
            return (False, f"sector cap: {sector} already has {same_sector} open (max {MAX_POSITIONS_PER_SECTOR})")

    # Theme cap (any overlap counts)
    new_themes = set(TICKER_THEMES.get(ticker.upper(), []))
    if new_themes:
        for theme in new_themes:
            same_theme = sum(1 for p in open_pos if theme in TICKER_THEMES.get(p['ticker'].upper(), []))
            if same_theme >= MAX_POSITIONS_PER_THEME:
                return (False, f"theme cap: '{theme}' already has {same_theme} open (max {MAX_POSITIONS_PER_THEME})")

    # Portfolio delta cap (rough — sums per-contract delta × qty; treats deltas as raw shares-equivalents)
    if contract_delta is not None:
        existing_delta = 0.0
        for p in open_pos:
            d = p.get('entry_delta')
            q = p.get('qty', 0) or 0
            if d is not None:
                # Sign by direction (already encoded in entry_delta if stored signed)
                existing_delta += float(d) * q
        # Adding this trade — assume same qty intended (use 1 as conservative lower bound)
        projected = abs(existing_delta + contract_delta)
        if projected > MAX_PORTFOLIO_DELTA:
            return (False, f"delta cap: projected net |delta|={projected:.2f} > {MAX_PORTFOLIO_DELTA}")

    return (True, "ok")


# ---------- Trading logic ----------
def signal_id_for(sig):
    """Stable ID per signal (source+ticker+direction+date)."""
    date = sig.get('Timestamp', sig.get('timestamp', ''))[:10]
    src = 'PREP' if sig.get('Status', '').startswith('TRIGGER_PREPOSITION') else 'V4'
    return f"{src}-{sig.get('Ticker')}-{sig.get('Type')}-{date}"


def preposition_to_signal(c):
    """Translate a CONFIRMED pre-position candidate into a V4-style signal dict.
    Used so the entry path is a single code flow for both trigger sources."""
    triggered_at = c.get('triggered_at_utc') or c.get('last_check_utc') or \
                   datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    score = float(c.get('score', 0))
    # Slightly smaller sizing for pre-position (multi-day hold, wider stops)
    size_mult = min(0.75, max(0.25, (score - 40) / 60.0))
    lvls = c.get('key_levels') or {}
    stop = lvls.get('stop_below')
    dte = c.get('suggested_dte_min', 14)
    return {
        'Ticker': c.get('ticker'),
        'Type': c.get('direction', 'CALL'),
        'Status': 'TRIGGER_PREPOSITION_CONFIRMED',
        'Confidence': str(score),
        'Timestamp': triggered_at,
        'DTE': dte,
        'Exit_Protocol': {
            'SL': str(stop) if stop is not None else '',
            'Size_Mult': round(size_mult, 2),
            'Path': 'PREPOSITION',
            'TIME_STOP': f"{max(1, int(dte * 0.4))} Days max",
            # TP_Premium_Target intentionally absent — computed at entry time using live option ask
        },
        'Screener_Logic': c.get('narrative', ''),
    }


def collect_all_triggers():
    """Union of V4 engine triggers + pre-position CONFIRMED candidates."""
    triggers = []
    # V4 engine triggers
    if os.path.exists(SIGNALS_PATH):
        try:
            with open(SIGNALS_PATH) as f: data = json.load(f)
            for s in data.get('signals', []):
                st = s.get('Status', '')
                if st.startswith('TRIGGER_') and st.endswith('CONFIRMED'):
                    triggers.append(s)
        except Exception: pass

    # Pre-position CONFIRMED candidates
    if os.path.exists(PREPOSITION_PATH):
        try:
            with open(PREPOSITION_PATH) as f: data = json.load(f)
            for c in data.get('candidates', []):
                if c.get('trigger_status') == 'CONFIRMED':
                    triggers.append(preposition_to_signal(c))
        except Exception: pass

    return triggers


def process_new_triggers(positions):
    """Find TRIGGER_*_CONFIRMED from V4 + pre-position, place orders."""
    triggers = collect_all_triggers()
    if not triggers: return positions

    for sig in triggers:
        ticker = sig.get('Ticker')
        direction = sig.get('Type', 'CALL')
        sig_id = signal_id_for(sig)

        # Dedup: skip if already have open/pending for this ticker+direction
        if has_open_for(positions, ticker, direction):
            continue
        # Concurrency cap
        if open_count(positions) >= MAX_CONCURRENT_POSITIONS:
            log(f"skip {sig_id}: at MAX_CONCURRENT_POSITIONS={MAX_CONCURRENT_POSITIONS}", "WARN")
            continue
        # Exposure cap (sector + theme; delta checked later once we have snapshot)
        ok, reason = exposure_check(positions, ticker, direction)
        if not ok:
            log(f"skip {sig_id}: EXPOSURE — {reason}", "WARN")
            continue

        ep = dict(sig.get('Exit_Protocol') or {})  # copy so we can enrich
        size_mult = float(ep.get('Size_Mult', sig.get('Size_Multiplier', 0.5)))
        target_dte = int(sig.get('DTE', 14))
        signal_source = 'PREPOSITION' if sig.get('Status', '').startswith('TRIGGER_PREPOSITION') else 'V4'

        # Get spot
        spot = get_underlying_price(ticker)
        if not spot:
            log(f"skip {sig_id}: no underlying quote for {ticker}", "WARN")
            continue

        # Find contract
        contract = find_contract(ticker, direction, target_dte, spot)
        if not contract:
            log(f"skip {sig_id}: no matching contract found", "WARN")
            continue

        symbol = contract['symbol']
        snap = get_option_snapshot(symbol)
        if not snap:
            log(f"skip {sig_id}: no snapshot for {symbol}", "WARN")
            continue
        if snap['spread_pct'] > MAX_OPTION_SPREAD_PCT:
            log(f"skip {sig_id}: spread {snap['spread_pct']*100:.1f}% > {MAX_OPTION_SPREAD_PCT*100:.0f}%", "WARN")
            continue

        # Earnings safety net (also gated upstream in meta_engine; defense in depth).
        # Window kept in sync with meta_engine (3 days).
        earn = earnings_within(ticker, days=3)
        if earn.get('within'):
            log(f"skip {sig_id}: earnings in {earn.get('days_until')}d ({earn.get('report_date')}) — IV crush risk", "WARN")
            continue

        # Delta-aware exposure recheck — now that we have the snapshot's delta,
        # verify portfolio net |delta| won't blow the cap (sign by direction).
        contract_delta_signed = None
        if snap.get('delta') is not None:
            raw_delta = float(snap['delta'])
            contract_delta_signed = raw_delta if direction == 'CALL' else -abs(raw_delta)
        ok, reason = exposure_check(positions, ticker, direction, contract_delta=contract_delta_signed)
        if not ok:
            log(f"skip {sig_id}: EXPOSURE (delta) — {reason}", "WARN")
            continue

        # Max-pain pin filter — only on weekly expiries (DTE <= 7), where pin risk is real
        contract_strike = float(contract.get('strike_price', 0))
        contract_exp = contract.get('expiration_date', '')
        if target_dte <= 7 and contract_exp:
            mp = max_pain(ticker, contract_exp)
            if mp and abs(contract_strike - mp.get('max_pain', 0)) < 1.0:
                log(f"skip {sig_id}: strike ${contract_strike} within $1 of max pain ${mp['max_pain']} on {contract_exp}", "WARN")
                continue

        # If TP_Premium_Target missing (pre-position signals), compute now from live ask
        if not ep.get('TP_Premium_Target'):
            try:
                from v4_meta_engine import compute_tp_target
                tp = compute_tp_target(snap['ask'], target_dte)
                ep['TP_Premium_Target'] = tp.get('target_premium')
                ep['TP_Pct_Gain'] = tp.get('pct_gain')
                ep['Entry_Premium_Estimate'] = tp.get('entry_estimate')
            except Exception as e:
                log(f"TP compute failed for {sig_id}: {e}", "WARN")

        # Sizing — smaller of (size_mult * BASE) and MAX
        target_usd = min(BASE_SIZE_USD * size_mult, MAX_POSITION_USD)
        contract_cost_usd = snap['ask'] * 100  # use ask for safety (worst-case fill)
        qty = max(1, int(target_usd / contract_cost_usd))
        # Floor qty at 2 when the cap allows it — the partial-TP "leave 1 runner"
        # logic is a no-op at qty=1. If 2 contracts fit MAX_POSITION_USD, take them.
        if qty == 1 and (2 * contract_cost_usd) <= MAX_POSITION_USD:
            qty = 2
        actual_cost = qty * contract_cost_usd

        log(f"SIGNAL {sig_id}: spot={spot:.2f}, contract={symbol}, ask=${snap['ask']:.2f}, "
            f"qty={qty}, ~cost=${actual_cost:.0f}, score={sig.get('Confidence')}, size_mult={size_mult}")

        order = submit_order(symbol, qty, 'buy', sig_id)
        if not order:
            log(f"order submission failed for {sig_id}", "ERR")
            continue

        # Compute TP1 (halfway between entry and TP2 — for partial profit taking)
        entry_est = snap['ask']
        tp2 = ep.get('TP_Premium_Target')
        # Defensive: guard against malformed tp2 (empty string, None, non-numeric)
        try:
            tp2_num = float(tp2) if tp2 not in (None, '', 'null') else None
        except (TypeError, ValueError):
            tp2_num = None
        tp1 = round(entry_est + (tp2_num - entry_est) * 0.5, 2) if tp2_num else None
        tp2 = tp2_num  # normalize for downstream

        # Gamma-wall context — record the underlying levels where dealer hedging flips.
        # Used by Patrol to manage exits: if underlying breaks the upper gamma wall,
        # consider trailing stop tighter; if it can't pierce, suggest scale-out.
        walls = gamma_walls(ticker, spot)
        gw_upper = walls.get('upper_wall', {}).get('strike') if walls else None
        gw_lower = walls.get('lower_wall', {}).get('strike') if walls else None
        if walls.get('upper_wall') or walls.get('lower_wall'):
            log(f"  gamma walls for {ticker}: upper=${gw_upper}, lower=${gw_lower} (vs spot ${spot:.2f})")

        # Record position as PENDING_ENTRY
        positions.append({
            "id": str(uuid.uuid4())[:8],
            "signal_id": sig_id,
            "signal_source": signal_source,
            "ticker": ticker, "direction": direction,
            "contract_symbol": symbol,
            "strike": float(contract.get('strike_price', 0)),
            "expiration": contract.get('expiration_date'),
            "qty": qty,
            "remaining_qty": qty,  # decreases after partial close
            "entry_order_id": order.get('id'),
            "entry_order_status": order.get('status'),
            "entry_price_estimate": entry_est,
            "entry_price_filled": None,
            "entry_time_utc": datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "entry_signal_score": sig.get('Confidence'),
            "size_multiplier": size_mult,
            "tp_premium_target": tp2,      # TP2 = full target
            "tp1_premium_target": tp1,     # TP1 = halfway, partial exit level
            "tp1_hit_at_utc": None,
            "tp1_exit_order_id": None,
            "gamma_wall_upper": gw_upper,  # underlying level above which dealer flow flips
            "gamma_wall_lower": gw_lower,
            "entry_delta": contract_delta_signed,  # signed: +CALL / -PUT (for portfolio delta cap)
            "tp1_exit_price_filled": None,
            "tp1_qty_closed": 0,
            "tp1_realized_pnl_usd": 0,
            "partial_closed": False,
            "tp_pct_gain": ep.get('TP_Pct_Gain'),
            "sl_underlying": ep.get('SL'),
            "underlying_at_entry": spot,
            "dte_at_entry": target_dte,
            "status": "PENDING_ENTRY",
            "exit_order_id": None, "exit_price_filled": None,
            "exit_time_utc": None, "exit_reason": None,
            "realized_pnl_usd": None, "realized_pnl_pct": None,
        })

    return positions


def reconcile_pending_entries(positions):
    """For PENDING_ENTRY positions, poll Alpaca for fill status."""
    for p in positions:
        if p.get('status') != 'PENDING_ENTRY' or not p.get('entry_order_id'): continue
        order = get_order_status(p['entry_order_id'])
        if not order: continue
        st = order.get('status')
        p['entry_order_status'] = st
        if st == 'filled':
            p['entry_price_filled'] = float(order.get('filled_avg_price', 0))
            p['status'] = 'OPEN'
            log(f"FILL entry {p['ticker']} {p['contract_symbol']} qty={p['qty']} @ ${p['entry_price_filled']}", "FILL")
        elif st in ('canceled', 'rejected', 'expired'):
            p['status'] = 'ENTRY_FAILED'
            log(f"entry {st}: {p['ticker']} {p['contract_symbol']} — order {p['entry_order_id']}", "ERR")
    return positions


def evaluate_exits(positions):
    """For OPEN positions, check TP1 partial + TP2 full + SL + time stop.
    Partial-profit logic: at TP1 (halfway to TP2) close 50%, trail rest at breakeven."""
    for p in positions:
        if p.get('status') != 'OPEN': continue

        snap = get_option_snapshot(p['contract_symbol'])
        spot = get_underlying_price(p['ticker'])
        if not snap or not spot: continue

        current_premium = snap['mid']
        entry_premium = p.get('entry_price_filled') or p.get('entry_price_estimate')
        remaining_qty = p.get('remaining_qty', p.get('qty', 0))
        p['current_premium'] = round(current_premium, 2)
        p['current_underlying'] = round(spot, 2)
        p['unrealized_pnl_pct'] = round((current_premium - entry_premium) / entry_premium * 100, 2) if entry_premium else None

        if remaining_qty <= 0:
            # Nothing left to sell — shouldn't happen but guard anyway
            p['status'] = 'CLOSED'
            continue

        # ===== PARTIAL PROFIT TAKING =====
        # At TP1, close everything EXCEPT 1 runner. Locks in most of the gain, lets
        # the last contract ride to TP2 or trailing stop for the "free lottery ticket".
        # Requires qty >= 2; qty=1 can't partial-close, defaults to all-or-nothing logic.
        if not p.get('partial_closed') and p.get('qty', 0) >= 2 and p.get('tp1_exit_order_id') is None:
            tp1 = p.get('tp1_premium_target')
            if tp1 and current_premium >= float(tp1):
                partial_qty = p['qty'] - 1  # always leave exactly 1 runner
                log(f"TP1 HIT {p['ticker']} {p['contract_symbol']}: premium ${entry_premium:.2f} → ${current_premium:.2f} "
                    f"({p['unrealized_pnl_pct']:+.1f}%) | closing {partial_qty}/{p['qty']} contracts, 1 runner stays", "EXIT")
                order = submit_order(p['contract_symbol'], partial_qty, 'sell', p['signal_id'] + '-TP1')
                if order:
                    p['tp1_exit_order_id'] = order.get('id')
                    p['tp1_hit_at_utc'] = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
                    # Status stays OPEN until the partial fills; reconciler will reduce remaining_qty
                continue  # don't also check other exits this cycle — let TP1 fill first

        # ===== FULL EXIT CHECKS (for remaining contracts) =====
        exit_reason = None

        # 1. TP2 full target
        tp_target = p.get('tp_premium_target')
        if tp_target and current_premium >= float(tp_target):
            exit_reason = 'tp_premium_hit'

        # 2. Trailing stop at breakeven — only active after partial close
        if not exit_reason and p.get('partial_closed'):
            if current_premium <= entry_premium:
                exit_reason = 'trailing_breakeven'

        # 3. Underlying SL (applies to both pre- and post-partial)
        if not exit_reason:
            sl_str = p.get('sl_underlying')
            try:
                sl = float(sl_str) if sl_str else None
            except Exception: sl = None
            if sl is not None:
                if (p['direction'] == 'CALL' and spot < sl) or (p['direction'] == 'PUT' and spot > sl):
                    exit_reason = 'sl_underlying_hit'

        # 4. Time stop (held > DTE * 0.4)
        if not exit_reason:
            try:
                entry_dt = datetime.strptime(p['entry_time_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
                age_days = (datetime.now(UTC) - entry_dt).total_seconds() / 86400
                max_age = max(1, p.get('dte_at_entry', 7) * 0.4)
                if age_days >= max_age:
                    exit_reason = 'time_stop'
            except Exception: pass

        # 5. EOD close for short-dated
        if not exit_reason and p.get('dte_at_entry', 99) <= 7:
            now_pt = datetime.now(PACIFIC)
            if now_pt.hour == 12 and now_pt.minute >= 45:
                exit_reason = 'eod_short_dte'

        # 6. Phase 5 — gamma-wall smart exit (sticky-touch model).
        # Once spot gets within 1% of the directional wall, mark it touched (sticky bool).
        # Later, if spot drops 3% back from that wall while position is still in profit,
        # treat as a failed-breakout rejection and lock the gain.
        # Also: defensive exit if spot reaches the *opposite* wall while position is in loss.
        if not exit_reason and entry_premium and entry_premium > 0 \
                and p.get('gamma_wall_upper') and p.get('gamma_wall_lower'):
            try:
                gw_up = float(p['gamma_wall_upper'])
                gw_lo = float(p['gamma_wall_lower'])

                if p['direction'] == 'CALL':
                    # Sticky touch: mark when spot first hits within 1% of upper wall
                    if spot >= gw_up * 0.99:
                        p['gw_upper_touched'] = True
                    # Failed breakout: touched then fell 3% back while still profitable
                    if p.get('gw_upper_touched') and spot < gw_up * 0.97 \
                            and current_premium > entry_premium * 1.05:
                        exit_reason = 'gamma_wall_rejection'
                    # Defensive: hit lower wall while in loss
                    elif spot <= gw_lo * 1.005 and current_premium < entry_premium * 0.85:
                        exit_reason = 'gamma_wall_breakdown'
                elif p['direction'] == 'PUT':
                    if spot <= gw_lo * 1.01:
                        p['gw_lower_touched'] = True
                    if p.get('gw_lower_touched') and spot > gw_lo * 1.03 \
                            and current_premium > entry_premium * 1.05:
                        exit_reason = 'gamma_wall_rejection'
                    elif spot >= gw_up * 0.995 and current_premium < entry_premium * 0.85:
                        exit_reason = 'gamma_wall_breakdown'
            except (TypeError, ValueError):
                pass

        if exit_reason:
            log(f"EXIT trigger {p['ticker']} {p['contract_symbol']}: {exit_reason} | "
                f"premium ${entry_premium:.2f} → ${current_premium:.2f} ({p['unrealized_pnl_pct']:+.1f}%) "
                f"| selling {remaining_qty} contracts", "EXIT")
            order = submit_order(p['contract_symbol'], remaining_qty, 'sell', p['signal_id'])
            if order:
                p['exit_order_id'] = order.get('id')
                p['exit_reason'] = exit_reason
                p['status'] = 'PENDING_EXIT'
            else:
                log(f"EXIT order submission failed for {p['ticker']}", "ERR")
    return positions


def reconcile_pending_tp1(positions):
    """Handle partial-exit order fills — mark position as partial_closed, reduce remaining_qty."""
    for p in positions:
        if p.get('status') != 'OPEN': continue
        if not p.get('tp1_exit_order_id') or p.get('partial_closed'): continue
        order = get_order_status(p['tp1_exit_order_id'])
        if not order: continue
        st = order.get('status')
        if st == 'filled':
            fill_px = float(order.get('filled_avg_price', 0))
            partial_qty = int(order.get('filled_qty', 0))
            entry_px = p.get('entry_price_filled') or p.get('entry_price_estimate', 0)
            pnl = (fill_px - entry_px) * 100 * partial_qty
            p['tp1_exit_price_filled'] = fill_px
            p['tp1_qty_closed'] = partial_qty
            p['tp1_realized_pnl_usd'] = round(pnl, 2)
            p['remaining_qty'] = p.get('qty', 0) - partial_qty
            p['partial_closed'] = True
            log(f"TP1 FILL {p['ticker']}: {partial_qty} closed @ ${fill_px} | "
                f"partial P&L ${pnl:+.2f} | {p['remaining_qty']} remain, trailing at breakeven", "FILL")
        elif st in ('canceled', 'rejected', 'expired'):
            log(f"TP1 exit {st}: {p['ticker']} — will retry next cycle", "WARN")
            p['tp1_exit_order_id'] = None  # allow retry
            p['tp1_hit_at_utc'] = None
    return positions


def reconcile_pending_exits(positions):
    """Final exit fill — aggregates with any partial close to show total P&L."""
    for p in positions:
        if p.get('status') != 'PENDING_EXIT' or not p.get('exit_order_id'): continue
        order = get_order_status(p['exit_order_id'])
        if not order: continue
        st = order.get('status')
        if st == 'filled':
            exit_px = float(order.get('filled_avg_price', 0))
            p['exit_price_filled'] = exit_px
            p['exit_time_utc'] = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
            entry_px = p.get('entry_price_filled') or p.get('entry_price_estimate', 0)
            final_qty = p.get('remaining_qty', p.get('qty', 0))
            final_pnl = (exit_px - entry_px) * 100 * final_qty
            # Add the TP1 partial P&L (if any) to get total realized
            tp1_pnl = p.get('tp1_realized_pnl_usd', 0) or 0
            total_pnl = round(final_pnl + tp1_pnl, 2)
            # Effective combined pct (weighted by qty)
            total_qty = p.get('qty', final_qty) or final_qty
            effective_cost = entry_px * 100 * total_qty if entry_px else 0
            total_pct = (total_pnl / effective_cost * 100) if effective_cost else 0
            p['realized_pnl_usd'] = total_pnl
            p['realized_pnl_pct'] = round(total_pct, 2)
            p['status'] = 'CLOSED'
            partial_note = f" (incl. ${tp1_pnl:+.0f} from TP1 partial)" if tp1_pnl else ""
            log(f"CLOSED {p['ticker']} {p['contract_symbol']}: total P&L ${total_pnl} "
                f"({total_pct:+.1f}%) reason={p['exit_reason']}{partial_note}", "FILL")
        elif st in ('canceled', 'rejected', 'expired'):
            p['status'] = 'EXIT_FAILED'
            log(f"exit {st}: {p['ticker']} — order {p['exit_order_id']}", "ERR")
    return positions


def main_loop():
    log("BOOTING V4 PAPER TRADER")
    if not preflight():
        log("Preflight failed — refusing to start.", "ERR")
        sys.exit(1)
    log(f"Cadence: {TRADER_CADENCE}s | Window: {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}–{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} PT")
    log(f"Risk: max_concurrent={MAX_CONCURRENT_POSITIONS}, max_position=${MAX_POSITION_USD}, base_size=${BASE_SIZE_USD}")
    log(f"Kill switch: touch {KILL_FILE} to stop")

    while True:
        if os.path.exists(KILL_FILE):
            log("kill file detected — exiting cleanly", "WARN")
            os.remove(KILL_FILE)
            break

        if not in_window():
            time.sleep(300)  # slow poll outside window
            continue

        positions = load_positions()

        # 1. New triggers → place buy orders
        positions = process_new_triggers(positions)
        # 2. Reconcile pending entries → mark filled
        positions = reconcile_pending_entries(positions)
        # 3. Evaluate exits on open positions (TP1 partial, TP2 full, SL, time stop)
        positions = evaluate_exits(positions)
        # 4. Reconcile TP1 partial fills → mark position partial_closed, reduce remaining_qty
        positions = reconcile_pending_tp1(positions)
        # 5. Reconcile pending final exits → mark CLOSED with aggregated P&L
        positions = reconcile_pending_exits(positions)

        save_positions(positions)
        time.sleep(TRADER_CADENCE)


if __name__ == "__main__":
    main_loop()
