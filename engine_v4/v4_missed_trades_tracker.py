"""
V4 MISSED TRADES TRACKER — logs setups the system saw but didn't take, then
measures their actual forward outcomes.

Specifically tracks the "preposition vs intraday flow conflict" pattern:
  Patrol confirms a preposition breakout (multi-day setup said BUY, price
  broke the trigger, held 3 bars) BUT the meta engine rejects the resulting
  signal at the next pipeline cycle (live flow was bid-side, premium too
  low, score too low, etc).

This is the MRVL-on-Friday case: setup was right (+12-23% in backtest),
patrol confirmed 8 times, every one got rejected by the live-flow gate.

Cross-references:
  v4_patrol_log.jsonl  → state transitions (find CONFIRMED events)
  v4_ledger.json       → meta-engine rejections at same timestamp
  Alpaca bars          → forward returns 1d / 3d / 5d after rejection

Output:
  v4_missed_trades.json — list of conflicts with outcomes
  Console — ranking by missed-alpha (biggest opportunities skipped)

After 2-3 weeks of live data, we'll have empirical evidence:
  - If missed trades on average win → meta-engine gate is over-restrictive
  - If missed trades on average lose → gate is correctly filtering
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATROL_LOG = os.path.join(BASE_DIR, 'v4_patrol_log.jsonl')
LEDGER_PATH = os.path.join(BASE_DIR, 'v4_ledger.json')
PREP_PATH = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')
OUT_PATH = os.path.join(BASE_DIR, 'v4_missed_trades.json')

REJECT_REASONS_OF_INTEREST = {
    'REJECTED_BID_SIDE',
    'REJECTED_LOW_PREMIUM',
    'REJECTED_LOW_SCORE',
    'REJECTED_NO_FLOW',
    'REJECTED_OI_UNWIND',
    'REJECTED_EARNINGS_NEAR',
    'REJECTED_TREND_MISMATCH',
}


def load_patrol_confirmations():
    """Find every preposition CONFIRMED transition in the patrol log."""
    if not os.path.exists(PATROL_LOG): return []
    confirms = []
    with open(PATROL_LOG) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                e = json.loads(line)
                if e.get('to_status') == 'CONFIRMED':
                    confirms.append(e)
            except: continue
    return confirms


def load_rejections_by_ticker_date():
    """Map (ticker, YYYY-MM-DD) → list of rejection events from ledger."""
    if not os.path.exists(LEDGER_PATH): return {}
    with open(LEDGER_PATH) as f: ledger = json.load(f)
    by_key = defaultdict(list)
    for e in ledger:
        status = e.get('Status', '')
        if status not in REJECT_REASONS_OF_INTEREST: continue
        ts = e.get('Timestamp', '')
        date = ts[:10] if ts else ''
        key = (e.get('Ticker'), date)
        by_key[key].append({
            'timestamp': ts,
            'status': status,
            'confidence': e.get('Confidence'),
            'screener_logic': e.get('Screener_Logic', ''),
        })
    return by_key


def get_forward_returns(ticker: str, ref_date: str) -> dict:
    """1d / 3d / 5d forward stock returns from ref_date open.
    Note: option-premium would be more accurate but we don't have the
    contract symbol for missed trades — falls back to underlying."""
    try:
        ref = datetime.strptime(ref_date, '%Y-%m-%d').replace(tzinfo=UTC)
    except: return {'1D': None, '3D': None, '5D': None}

    end = ref + timedelta(days=12)
    df = fetch_alpaca_bars(ticker, '1Day',
        ref.strftime('%Y-%m-%dT%H:%M:%SZ'),
        end.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty: return {'1D': None, '3D': None, '5D': None}
    try:
        entry = float(df['Open'].iloc[0])
        out = {}
        for h in (1, 3, 5):
            if len(df) > h:
                exit_close = float(df['Close'].iloc[h])
                out[f'{h}D'] = (exit_close - entry) / entry if entry > 0 else None
            else:
                out[f'{h}D'] = None
        return out
    except: return {'1D': None, '3D': None, '5D': None}


def main():
    print("\n" + "="*82)
    print("V4 MISSED TRADES TRACKER — preposition CONFIRMED but rejected by meta engine")
    print("="*82)

    confirms = load_patrol_confirmations()
    print(f"\nPreposition CONFIRMED transitions in patrol log: {len(confirms)}")

    rejections = load_rejections_by_ticker_date()
    print(f"Rejection events in ledger (filterable reasons): "
          f"{sum(len(v) for v in rejections.values())}")

    # Build missed-trade records: each unique (ticker, date) where patrol confirmed
    # AND meta engine rejected on that day, with forward returns.
    seen_dates = set()
    missed = []
    for c in confirms:
        ticker = c.get('ticker')
        ts = c.get('ts_utc', '')
        date = ts[:10] if ts else ''
        if not ticker or not date: continue
        key = (ticker, date)
        if key in seen_dates: continue  # dedupe per ticker per day
        seen_dates.add(key)

        # Was there a meta-engine rejection for this ticker on this date?
        rejs = rejections.get(key, [])
        if not rejs: continue

        # Get forward returns from confirmation date
        fwd = get_forward_returns(ticker, date)

        # Direction: from patrol log
        direction = c.get('direction', 'CALL')
        # Apply directional sign
        sign = 1 if direction == 'CALL' else -1
        signed_fwd = {h: sign * v if v is not None else None for h, v in fwd.items()}

        missed.append({
            'ticker': ticker,
            'date': date,
            'direction': direction,
            'patrol_confirmed_at': ts,
            'spot_at_confirm': c.get('spot'),
            'trigger_level': c.get('trigger'),
            'rejection_reasons': [r['status'] for r in rejs],
            'rejection_count': len(rejs),
            'last_rejection_logic': rejs[-1].get('screener_logic', '')[:150],
            'fwd_returns_signed': signed_fwd,  # + means thesis worked
            'fwd_returns_raw': fwd,
        })

    # Sort by missed-alpha (biggest 5D winner first)
    missed.sort(key=lambda x: -(x['fwd_returns_signed'].get('5D') or 0))

    # Console report
    print(f"\n=== MISSED TRADES (preposition right, meta engine vetoed) ===")
    print(f"{'ticker':<7} {'date':<12} {'dir':<5} {'1D':>7} {'3D':>7} {'5D':>7} {'rejections':<35} ")
    print('-' * 95)
    for m in missed[:25]:
        f = m['fwd_returns_signed']
        d1 = f"{f['1D']*100:+.1f}%" if f['1D'] is not None else '—'
        d3 = f"{f['3D']*100:+.1f}%" if f['3D'] is not None else '—'
        d5 = f"{f['5D']*100:+.1f}%" if f['5D'] is not None else '—'
        rejs = ','.join(set(m['rejection_reasons']))[:34]
        print(f"{m['ticker']:<7} {m['date']:<12} {m['direction']:<5} {d1:>7} {d3:>7} {d5:>7} {rejs:<35}")

    # Aggregate verdict
    if missed:
        rets_5d = [m['fwd_returns_signed'].get('5D') for m in missed if m['fwd_returns_signed'].get('5D') is not None]
        if rets_5d:
            wins = sum(1 for r in rets_5d if r > 0.005)
            losses = sum(1 for r in rets_5d if r < -0.005)
            n = len(rets_5d)
            avg = sum(rets_5d) / n
            print(f"\n=== AGGREGATE — would-have-been outcomes ===")
            print(f"  n: {n}")
            print(f"  wins (>0.5%):  {wins} ({wins/n*100:.1f}%)")
            print(f"  losses (<0.5%): {losses} ({losses/n*100:.1f}%)")
            print(f"  avg 5D return: {avg*100:+.2f}%")
            print()
            if wins > losses * 1.5:
                print(f"  🟢 VERDICT: missed trades skew WINNERS — meta-engine gate may be over-restrictive")
            elif losses > wins * 1.5:
                print(f"  🔴 VERDICT: missed trades skew LOSERS — gate is doing its job")
            else:
                print(f"  ⚪ VERDICT: roughly even — need more samples")

    # Persist
    with open(OUT_PATH, 'w') as f:
        json.dump({
            'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'total_missed': len(missed),
            'records': missed,
        }, f, indent=2, default=str)
    print(f"\n💾 Saved: {OUT_PATH}")


if __name__ == "__main__":
    main()
