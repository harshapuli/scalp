"""
V4 ELITE FORENSICS DOSSIER & SHADOW AUDIT (v3)

What changed vs v2:
- NEW: option-premium forward returns. The prior analyzer measured stock
  returns, but P&L is realized on option premium. Stock +1% can mean option
  +50% (near-ATM) or flat (deep OTM). Now: if a record has Contract_Symbol,
  we pull Alpaca option bars and compute premium-based fwd returns as the
  authoritative source. Stock returns are retained as context.
- NEW: `return_basis` tag on every record — 'option_premium' or 'underlying_stock'.
- NEW: option-level aggregation of closed positions (from v4_paper_positions.json)
  so we can judge real P&L outcomes, not just stock proxies.

From v2:
- multi-window (1H/4H/EOD/D1), SPY baseline, per-filter verdicts, RTH advancement.
"""
import os
import sys
import json
import statistics
import requests
from datetime import datetime, timedelta, time as dtime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))
except Exception:
    pass

ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
_H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
      "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}

# Regular trading hours in UTC (US equities, post-DST agnostic — using EDT base 13:30Z–20:00Z)
RTH_OPEN_UTC = dtime(13, 30)
RTH_CLOSE_UTC = dtime(20, 0)
HORIZONS = ['1H', '4H', 'EOD', 'D1']  # measurement windows
DEFAULT_RETURN = {h: None for h in HORIZONS}

TRIGGER_STATUSES = {'TRIGGER_BREAKOUT', 'TRIGGER_PULLBACK',
                    'TRIGGER_BREAKOUT_CONFIRMED', 'TRIGGER_PULLBACK_CONFIRMED'}
WATCH_STATUSES = {'WATCH_BREAKOUT', 'WATCH_PULLBACK', 'WATCH_EXPIRED'}


def next_rth_open(t_utc: datetime) -> datetime:
    """Advance a UTC timestamp to the next regular session open if it's outside RTH or on a weekend."""
    candidate = t_utc
    for _ in range(5):  # at most skip a long weekend
        wd = candidate.weekday()  # Mon=0..Sun=6
        if wd >= 5:
            # Saturday/Sunday → jump to Monday open
            candidate = (candidate + timedelta(days=(7 - wd))).replace(
                hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
            continue
        t_only = candidate.time()
        if t_only < RTH_OPEN_UTC:
            return candidate.replace(hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
        if t_only >= RTH_CLOSE_UTC:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
            continue
        return candidate
    return t_utc  # fallback


def fetch_option_bars(contract_symbol: str, start: datetime, end: datetime, timeframe: str = '1Hour') -> list:
    """Return list of Alpaca option bars (dicts with t/o/h/l/c) in time order, or []."""
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v1beta1/options/bars",
            headers=_H,
            params={
                "symbols": contract_symbol, "timeframe": timeframe,
                "start": start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "end": end.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "limit": 200,
            },
            timeout=10,
        )
        if r.status_code != 200: return []
        return r.json().get('bars', {}).get(contract_symbol, []) or []
    except Exception:
        return []


def compute_option_returns(contract_symbol: str, entry_ts_utc: datetime, entry_price: float) -> dict:
    """Compute forward premium returns (1H/4H/EOD/D1) from entry_price using option bars.
    Returns {fwd_ret: {...}, mfe: {...}, mae: {...}, data_status: 'OK'|...}.
    For illiquid contracts (no bars), returns data_status='NO_OPTION_BARS'."""
    out = {'fwd_ret': dict(DEFAULT_RETURN), 'mfe': dict(DEFAULT_RETURN),
           'mae': dict(DEFAULT_RETURN), 'data_status': 'OK'}
    if not contract_symbol or not entry_price or entry_price <= 0:
        out['data_status'] = 'MISSING_INPUTS'
        return out

    horizons_hours = {'1H': 1, '4H': 4, 'EOD': 7, 'D1': 24}
    # Pull widest window once (D1) and slice per horizon
    bars = fetch_option_bars(contract_symbol, entry_ts_utc, entry_ts_utc + timedelta(hours=26))
    if not bars:
        out['data_status'] = 'NO_OPTION_BARS'
        return out

    # bars sorted by time; convert timestamps to datetime
    for b in bars:
        b['_dt'] = datetime.strptime(b['t'].replace('Z',''), '%Y-%m-%dT%H:%M:%S') if 'T' in b['t'] else None

    for h, hrs in horizons_hours.items():
        window_end = entry_ts_utc + timedelta(hours=hrs)
        # Only measure if window has elapsed
        if datetime.utcnow() < window_end: continue
        in_window = [b for b in bars if b['_dt'] and b['_dt'] <= window_end]
        if not in_window: continue
        last_close = float(in_window[-1]['c'])
        high = max(float(b['h']) for b in in_window)
        low = min(float(b['l']) for b in in_window)
        out['fwd_ret'][h] = (last_close - entry_price) / entry_price
        out['mfe'][h] = (high - entry_price) / entry_price
        out['mae'][h] = (low - entry_price) / entry_price
    return out


def fetch_returns_window(ticker: str, start: datetime, hours_forward: int) -> tuple:
    """Pull 1H bars from start to start + hours_forward. Return (entry_price, mfe%, mae%, fwd_ret%)
    with returns measured on the underlying. Returns (0,0,0,None) if no data."""
    end = start + timedelta(hours=hours_forward)
    df = fetch_alpaca_bars(ticker, '1Hour',
                           start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           end.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty:
        return 0.0, 0.0, 0.0, None
    entry = float(df['Open'].iloc[0])
    if entry <= 0:
        return 0.0, 0.0, 0.0, None
    high = float(df['High'].max())
    low = float(df['Low'].min())
    last = float(df['Close'].iloc[-1])
    mfe = (high - entry) / entry
    mae = (low - entry) / entry
    fwd = (last - entry) / entry
    return entry, mfe, mae, fwd


def compute_multi_window(ticker: str, trade_type: str, entry_ts: str,
                         contract_symbol: str = None, option_entry_price: float = None) -> dict:
    """Compute MFE/MAE/forward returns across 1H/4H/EOD/D1 windows + SPY baselines.

    If contract_symbol + option_entry_price are provided, fwd_ret is measured on
    OPTION PREMIUM (authoritative for P&L) and stock returns are retained as
    secondary context under *_stock fields. Otherwise, falls back to stock returns.

    Sign-flips for PUTs so positive = good for the trade direction (stock returns only).
    """
    out = {
        'entry_price': 0.0, 'measurement_start_utc': None,
        'mfe': dict(DEFAULT_RETURN), 'mae': dict(DEFAULT_RETURN),
        'fwd_ret': dict(DEFAULT_RETURN), 'spy_fwd_ret': dict(DEFAULT_RETURN),
        'excess_ret': dict(DEFAULT_RETURN), 'data_status': 'OK',
        'return_basis': 'underlying_stock',  # flipped to 'option_premium' when option data is used
        # Stock-level context retained even when option returns are primary
        'stock_fwd_ret': dict(DEFAULT_RETURN), 'stock_mfe': dict(DEFAULT_RETURN), 'stock_mae': dict(DEFAULT_RETURN),
    }
    try:
        ts = datetime.strptime(entry_ts, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        out['data_status'] = 'BAD_TIMESTAMP'
        return out

    measurement_start = next_rth_open(ts)
    out['measurement_start_utc'] = measurement_start.strftime('%Y-%m-%dT%H:%M:%SZ')

    # If measurement start is in the future (analyzer ran before next session) bail gracefully
    if measurement_start > datetime.utcnow():
        out['data_status'] = 'PENDING_NEXT_SESSION'
        return out

    horizon_hours = {'1H': 1, '4H': 4, 'EOD': 7, 'D1': 24}
    # CALL or UNKNOWN → sign=+1 (interpret raw underlying direction); PUT → flip.
    # UNKNOWN happens for early Phase-1 rejections where flow type wasn't determined.
    sign = -1 if trade_type == 'PUT' else 1

    for h, hrs in horizon_hours.items():
        # Need full window elapsed before measuring
        if datetime.utcnow() < measurement_start + timedelta(hours=hrs):
            continue
        entry, mfe, mae, fwd = fetch_returns_window(ticker, measurement_start, hrs)
        if fwd is None:
            continue
        if h == '1H':
            out['entry_price'] = entry
        # Stock-level returns (always computed, for context)
        out['stock_fwd_ret'][h] = sign * fwd
        out['stock_mfe'][h] = (mfe if sign > 0 else -mae)
        out['stock_mae'][h] = (mae if sign > 0 else -mfe)

        _, _, _, spy_fwd = fetch_returns_window('SPY', measurement_start, hrs)
        if spy_fwd is not None:
            out['spy_fwd_ret'][h] = sign * spy_fwd
            out['excess_ret'][h] = out['stock_fwd_ret'][h] - out['spy_fwd_ret'][h]

    # Default: fwd_ret = stock_fwd_ret (backwards-compat for records without option data)
    out['fwd_ret'] = dict(out['stock_fwd_ret'])
    out['mfe'] = dict(out['stock_mfe'])
    out['mae'] = dict(out['stock_mae'])

    # If contract info is available, override fwd_ret with option-premium returns (authoritative)
    if contract_symbol and option_entry_price and option_entry_price > 0:
        opt = compute_option_returns(contract_symbol, measurement_start, option_entry_price)
        if opt['data_status'] == 'OK' and any(v is not None for v in opt['fwd_ret'].values()):
            out['fwd_ret'] = opt['fwd_ret']
            out['mfe'] = opt['mfe']
            out['mae'] = opt['mae']
            out['return_basis'] = 'option_premium'
            # excess_ret vs SPY doesn't directly apply to option premium; leave as stock-level excess.
        else:
            # Option bars unavailable — keep stock proxy but flag it
            out['data_status'] = f"OPTION_FALLBACK_{opt['data_status']}"

    return out


def diagnostic_statement(trade: dict, m: dict) -> str:
    """Human-readable forensic line for one ledger entry."""
    status = trade.get('Status', '')
    scr_logic = trade.get('Screener_Logic', '—')

    # Pick the most informative window we have data for
    fwd_4h = m['fwd_ret'].get('4H')
    fwd_eod = m['fwd_ret'].get('EOD')
    fwd_d1 = m['fwd_ret'].get('D1')
    excess_4h = m['excess_ret'].get('4H')
    primary = next((x for x in (fwd_4h, fwd_eod, fwd_d1) if x is not None), None)

    if m['data_status'] == 'PENDING_NEXT_SESSION':
        action_line = f"⏳ AWAITING DATA: rejection at {trade.get('Timestamp', '')[:16]} fell after RTH; measuring from next open."
    elif primary is None:
        action_line = f"⏳ AWAITING DATA: {m['data_status']} (window not yet elapsed)."
    elif status in TRIGGER_STATUSES:
        if primary > 0.005:
            action_line = f"🟢 TARGET HIT: +{primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
        elif primary < -0.005:
            action_line = f"🔴 LOSS: {primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
        else:
            action_line = f"⚪ FLAT: {primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
    elif status.startswith('REJECTED_'):
        # For rejections: positive fwd means filter MISSED a winner; negative means it AVOIDED a loser
        if excess_4h is not None and excess_4h <= -0.005:
            action_line = f"🛡️ FILTER VALIDATED: ticker underperformed SPY by {-excess_4h*100:.2f}% post-rejection."
        elif excess_4h is not None and excess_4h >= 0.01:
            action_line = f"⚠️ OVER-FILTERED: ticker outperformed SPY by +{excess_4h*100:.2f}% post-rejection."
        else:
            action_line = f"⚪ NEUTRAL: ticker tracked SPY ({primary*100:+.2f}% raw, excess {excess_4h*100 if excess_4h is not None else 0:+.2f}%)."
    elif status in WATCH_STATUSES:
        action_line = f"👁️ WATCH state ({status}); no entry executed."
    else:
        action_line = f"— {status}"

    horizons_str = " | ".join(f"{h}:{m['fwd_ret'][h]*100:+.2f}%" if m['fwd_ret'][h] is not None else f"{h}:—" for h in HORIZONS)

    return (f"[{trade['Ticker']} {trade.get('Type','?')}] {status}\n"
            f"   Screener     : {scr_logic}\n"
            f"   Forward (raw): {horizons_str}\n"
            f"   Verdict      : {action_line}")


def aggregate_filter_edge(rejection_records: list) -> dict:
    """Group rejections by filter, compute median excess return at 4H+EOD+D1, return verdict."""
    by_filter = {}
    for r in rejection_records:
        f = r['filter']
        by_filter.setdefault(f, {'excess_4h': [], 'excess_eod': [], 'excess_d1': [], 'count': 0})
        by_filter[f]['count'] += 1
        if r['excess'].get('4H') is not None: by_filter[f]['excess_4h'].append(r['excess']['4H'])
        if r['excess'].get('EOD') is not None: by_filter[f]['excess_eod'].append(r['excess']['EOD'])
        if r['excess'].get('D1') is not None: by_filter[f]['excess_d1'].append(r['excess']['D1'])

    out = {}
    for f, d in by_filter.items():
        med_4h = statistics.median(d['excess_4h']) if d['excess_4h'] else None
        med_eod = statistics.median(d['excess_eod']) if d['excess_eod'] else None
        med_d1 = statistics.median(d['excess_d1']) if d['excess_d1'] else None
        # Use the longest-horizon median that has >= 5 samples for the verdict
        chosen = None; chosen_label = None; chosen_n = 0
        for label, vals in (('D1', d['excess_d1']), ('EOD', d['excess_eod']), ('4H', d['excess_4h'])):
            if len(vals) >= 5:
                chosen = statistics.median(vals); chosen_label = label; chosen_n = len(vals); break
        if chosen is None:
            verdict = 'INSUFFICIENT_DATA'
        elif chosen <= -0.005:
            verdict = 'VALIDATED'
        elif chosen >= 0.005:
            verdict = 'OVER_GATING'
        else:
            verdict = 'NEUTRAL'
        out[f] = {
            'count': d['count'],
            'median_excess_4h': med_4h, 'median_excess_eod': med_eod, 'median_excess_d1': med_d1,
            'verdict': verdict, 'verdict_horizon': chosen_label, 'verdict_n': chosen_n,
        }
    return out


def build_contract_lookup():
    """Map (ticker, yyyy-mm-dd date) → (contract_symbol, entry_price_filled) from paper positions.
    Lets us enrich ledger entries with the actual contract that was traded on that day."""
    positions_path = os.path.join(os.path.dirname(__file__), 'v4_paper_positions.json')
    if not os.path.exists(positions_path): return {}
    with open(positions_path) as f: positions = json.load(f)
    lookup = {}
    for p in positions:
        t = p.get('ticker')
        cs = p.get('contract_symbol')
        ep = p.get('entry_price_filled')
        et = p.get('entry_time_utc', '')
        if not (t and cs and ep and et): continue
        date_key = et[:10]
        lookup[(t, date_key)] = (cs, ep)
        # Also keyed by ticker alone for same-day signal → position match
        lookup.setdefault((t, '*'), (cs, ep))
    return lookup


def run_daily_analysis():
    ledger_path = os.path.join(os.path.dirname(__file__), 'v4_ledger.json')
    shadow_path = os.path.join(os.path.dirname(__file__), 'v4_rejection_audit.json')

    if not os.path.exists(ledger_path):
        print("No trades logged in ledger.")
        return
    with open(ledger_path, 'r') as f:
        ledger = json.load(f)
    if not ledger:
        print("Ledger is empty.")
        return

    contract_lookup = build_contract_lookup()

    print("\n" + "=" * 80)
    print("🧠 V4 ELITE FORENSICS DOSSIER & SHADOW AUDIT (v2)")
    print("=" * 80 + "\n")

    triggers = []
    rejection_records = []  # for filter aggregation
    shadow_audit = []       # for JSON output

    for trade in ledger:
        # Resolve contract: prefer one recorded on the trade itself, else look up
        # the paper position we took on that ticker/day.
        contract = trade.get('Contract_Symbol')
        opt_entry = None
        if not contract:
            key = (trade.get('Ticker'), (trade.get('Timestamp','') or '')[:10])
            hit = contract_lookup.get(key) or contract_lookup.get((trade.get('Ticker'), '*'))
            if hit:
                contract, opt_entry = hit
        else:
            # If contract is on the trade but no price, try to pull from position
            hit = contract_lookup.get((trade.get('Ticker'), '*'))
            if hit and hit[0] == contract:
                opt_entry = hit[1]

        m = compute_multi_window(trade['Ticker'], trade.get('Type', 'CALL'),
                                 trade.get('Timestamp', ''),
                                 contract_symbol=contract, option_entry_price=opt_entry)
        statement = diagnostic_statement(trade, m)
        status = trade.get('Status', '')

        record = {
            'Ticker': trade['Ticker'], 'Type': trade.get('Type', '?'), 'Status': status,
            'Timestamp': trade.get('Timestamp'), 'Date': trade.get('Date'),
            'Confidence': trade.get('Confidence'),
            'Contract_Symbol': contract, 'Option_Entry_Price': opt_entry,
            'Return_Basis': m.get('return_basis'),
            'Measurement_Start_UTC': m['measurement_start_utc'],
            'Entry_Price': m['entry_price'],
            'MFE': m['mfe'], 'MAE': m['mae'],
            'Fwd_Ret': m['fwd_ret'],
            'Stock_Fwd_Ret': m.get('stock_fwd_ret'),
            'SPY_Fwd_Ret': m['spy_fwd_ret'],
            'Excess_Ret': m['excess_ret'],
            'Data_Status': m['data_status'],
            # Truth layer — propagate per-feature score breakdown for ablation analysis
            'Score_Matrix': trade.get('Score_Matrix') or {},
            'Screener_Logic': trade.get('Screener_Logic'),
        }

        if status in TRIGGER_STATUSES:
            triggers.append(record)
            print(statement); print("-" * 80)
        elif status.startswith('REJECTED_'):
            rejection_records.append({'filter': status, 'excess': m['excess_ret']})
            shadow_audit.append(record)
            print(statement); print("-" * 80)
        # WATCH_* statuses: log but don't analyze edge (no decision was taken)

    # Trigger aggregate — split by return_basis so we know which numbers are trustworthy
    if triggers:
        opt = [t for t in triggers if t.get('Return_Basis') == 'option_premium']
        stk = [t for t in triggers if t.get('Return_Basis') != 'option_premium']

        def _winrate(recs, horizon='EOD'):
            wins = sum(1 for t in recs if t['Fwd_Ret'].get(horizon) is not None and t['Fwd_Ret'][horizon] > 0.005)
            losses = sum(1 for t in recs if t['Fwd_Ret'].get(horizon) is not None and t['Fwd_Ret'][horizon] < -0.005)
            n = wins + losses
            return wins, losses, n, (wins/n*100) if n else 0

        print(f"\n📊 TRIGGER SUMMARY — split by return basis:")
        if opt:
            w,l,n,wr = _winrate(opt, 'EOD')
            print(f"  OPTION PREMIUM (authoritative, n={len(opt)}): {w}W / {l}L @ EOD → {wr:.1f}% win rate")
            # Median premium move
            moves = [t['Fwd_Ret'].get('EOD') for t in opt if t['Fwd_Ret'].get('EOD') is not None]
            if moves:
                print(f"    median EOD premium move: {statistics.median(moves)*100:+.1f}%")
        if stk:
            w,l,n,wr = _winrate(stk, 'EOD')
            print(f"  STOCK PROXY (n={len(stk)}, no option contract available): {w}W / {l}L @ EOD → {wr:.1f}% win rate")
    else:
        print("\n📊 TRIGGER SUMMARY: no triggers in ledger yet.")

    # Option-P&L summary from paper positions (actual trades, not ledger signals)
    positions_path = os.path.join(os.path.dirname(__file__), 'v4_paper_positions.json')
    if os.path.exists(positions_path):
        with open(positions_path) as f: positions = json.load(f)
        closed = [p for p in positions if p.get('status') == 'CLOSED' and p.get('realized_pnl_pct') is not None]
        openp = [p for p in positions if p.get('status') == 'OPEN' and p.get('unrealized_pnl_pct') is not None]
        if positions:
            print(f"\n💰 PAPER BOOK P&L (real option premium):")
            if closed:
                wins = sum(1 for p in closed if (p.get('realized_pnl_pct') or 0) > 0)
                total = sum(float(p.get('realized_pnl_usd') or 0) for p in closed)
                print(f"  closed: n={len(closed)}, wins={wins}, total realized P&L ${total:+.0f}")
            if openp:
                avg_unrl = statistics.mean(float(p.get('unrealized_pnl_pct') or 0) for p in openp)
                print(f"  open:   n={len(openp)}, avg unrealized {avg_unrl:+.1f}%")

    # Filter edge aggregation
    edge = aggregate_filter_edge(rejection_records)
    print("\n⚖️ REJECTION SHADOW BOOK — per-filter median excess return vs SPY")
    if not edge:
        print("  > No rejection data yet.")
    for f, d in edge.items():
        med = d.get('median_excess_4h')
        med_str = f"{med*100:+.2f}%" if med is not None else "—"
        verdict = d['verdict']
        if verdict == 'VALIDATED':
            tag = '🟢 VALIDATED'
        elif verdict == 'OVER_GATING':
            tag = '🔴 OVER-GATING'
        elif verdict == 'NEUTRAL':
            tag = '⚪ NEUTRAL'
        else:
            tag = '⏳ INSUFFICIENT (need 5+ samples)'
        h_used = d.get('verdict_horizon') or '—'
        n_used = d.get('verdict_n') or 0
        print(f"  > {tag} | {f} (n={d['count']}, verdict_n={n_used}@{h_used}) | excess_4h_median={med_str}")

    # Persist
    with open(shadow_path, 'w') as f:
        json.dump({
            'generated_utc': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'filter_edge': edge,
            'trigger_records': triggers,
            'shadow_records': shadow_audit,
        }, f, indent=2, default=str)
    print(f"\n💾 Shadow audit written to {shadow_path}")


if __name__ == "__main__":
    run_daily_analysis()
