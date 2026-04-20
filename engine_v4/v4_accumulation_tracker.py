"""
V4 ACCUMULATION SUCCESS TRACKER (Strategy B1)

Measures real-world outcomes for every accumulation scout we emitted:
  - Did the stock reach the breakout target (compression_high)? → HIT_TARGET
  - Did the stock hit the stop (compression_low × 0.99)? → HIT_STOP
  - Still in the compression range? → OPEN
  - For scouts that became paper positions: realized/unrealized option P&L

Inputs:
  v4_accumulation_signals.json       — current + historical emissions
  v4_accumulation_archive/YYYY-MM-DD.json  — daily snapshots (auto-rotated)
  v4_paper_positions.json            — which scouts became trades
  Alpaca bars                         — forward stock returns

Output:
  v4_accumulation_success.json       — per-scout outcome + aggregate stats

Usage:
  python3 engine_v4/v4_accumulation_tracker.py

Can be run:
  - In armada pipeline (daily)
  - Manually whenever you want fresh numbers
"""
import os
import sys
import json
import glob
import tempfile
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACC_SIGNALS_PATH = os.path.join(BASE_DIR, 'v4_accumulation_signals.json')
ACC_ARCHIVE_DIR = os.path.join(BASE_DIR, 'v4_accumulation_archive')
POSITIONS_PATH = os.path.join(BASE_DIR, 'v4_paper_positions.json')
OUT_PATH = os.path.join(BASE_DIR, 'v4_accumulation_success.json')
# Append-only outcome log — one line per (scout, evaluation_time) tuple.
# Accumulates forever so we can run pattern analysis across weeks of data.
OUTCOMES_HISTORY = os.path.join(BASE_DIR, 'v4_accumulation_outcomes_history.jsonl')

# Theme classifier — maps ticker → high-level narrative bucket. Used so we can
# track win rates per theme over time (AI vs memory vs crypto vs pharma etc).
# Extend this dict as new tickers appear; unknown tickers get 'Other'.
TICKER_THEMES = {
    # AI chip / compute
    'NVDA': 'AI compute', 'AMD': 'AI compute', 'AVGO': 'AI chips',
    'MRVL': 'AI chips (custom)', 'CRDO': 'AI chips (custom)',
    'ARM': 'AI chips', 'TSM': 'Semi foundry',
    # Memory
    'MU': 'Memory', 'SNDK': 'Memory', 'WDC': 'Memory',
    # Specialty semi
    'AXTI': 'Specialty semi', 'INTC': 'Legacy semi',
    # AI infrastructure
    'VRT': 'AI infra (data center)', 'COHR': 'AI infra (photonics)',
    'SMCI': 'AI infra (servers)', 'NBIS': 'AI cloud',
    # Mega-cap tech
    'AAPL': 'Mega tech', 'MSFT': 'Mega tech', 'GOOGL': 'Mega tech', 'GOOG': 'Mega tech',
    'META': 'Mega tech', 'AMZN': 'Mega tech', 'NFLX': 'Mega tech', 'PLTR': 'AI software',
    'ORCL': 'Mega tech', 'TSLA': 'AI/auto', 'IGV': 'Software ETF',
    # Crypto
    'COIN': 'Crypto', 'MSTR': 'Crypto', 'MARA': 'Bitcoin miner',
    'WULF': 'Bitcoin miner', 'IREN': 'Bitcoin miner', 'RIOT': 'Bitcoin miner',
    'CIFR': 'Bitcoin miner', 'IBIT': 'Crypto ETF', 'HOOD': 'Crypto-adjacent',
    # Space
    'RKLB': 'Space/Aerospace', 'SATS': 'Satellite comm', 'LMT': 'Defense',
    'BA': 'Aerospace', 'RTX': 'Defense',
    # Commodities / ETFs
    'GLD': 'Gold', 'SLV': 'Silver', 'USO': 'Oil', 'XOP': 'Oil',
    'TLT': 'Bonds', 'KWEB': 'China tech', 'FXI': 'China', 'QQQ': 'Tech ETF',
    'SPY': 'Broad ETF', 'DIA': 'Broad ETF', 'IWM': 'Small cap ETF',
    'KRE': 'Banks', 'JPM': 'Banks', 'GS': 'Banks', 'XLF': 'Financials',
    'XLU': 'Utilities', 'XHB': 'Homebuilders', 'XRT': 'Retail', 'XBI': 'Biotech',
    'HYG': 'Credit', 'ARKK': 'Growth',
    # Pharma / healthcare
    'LLY': 'Pharma', 'UNH': 'Health insurance', 'CVS': 'Pharma retail',
    'WBA': 'Pharma retail',
    # Energy / misc
    'XOM': 'Energy',
    # Small/mid growth
    'HOOD': 'Fintech', 'FRMI': 'Small cap',
}

def classify_theme(ticker: str) -> str:
    return TICKER_THEMES.get((ticker or '').upper(), 'Other')


def atomic_write(path, payload):
    dir_path = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
        raise


def collect_all_scouts():
    """Load current + archived scout emissions, dedup by (ticker, direction, emit_date)."""
    seen = set()
    scouts = []
    sources = []

    if os.path.exists(ACC_SIGNALS_PATH):
        sources.append(ACC_SIGNALS_PATH)
    if os.path.isdir(ACC_ARCHIVE_DIR):
        sources.extend(sorted(glob.glob(os.path.join(ACC_ARCHIVE_DIR, '*.json'))))

    for path in sources:
        try:
            with open(path) as f: data = json.load(f)
            for s in data.get('signals', []):
                key = (s.get('ticker'), s.get('direction'),
                       (s.get('generated_utc', '') or '')[:10])
                if key in seen: continue
                seen.add(key)
                scouts.append(s)
        except Exception: continue
    return scouts


def evaluate_stock_outcome(scout):
    """Fetch stock bars from emit_date forward, compare to stop/target.
    Returns {'outcome': HIT_TARGET|HIT_STOP|OPEN|NO_DATA, 'days_elapsed', 'stock_pct_move', 'peak_pct', 'trough_pct'}."""
    ticker = scout.get('ticker')
    emit_utc = scout.get('generated_utc', '')
    entry_ref = scout.get('entry_price_ref')
    stop = scout.get('stop_below')
    target = scout.get('target_above')
    if not (ticker and emit_utc and entry_ref and stop and target):
        return {'outcome': 'NO_DATA', 'reason': 'missing fields'}

    try:
        emit_dt = datetime.strptime(emit_utc.replace('Z', ''), '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)
    except Exception:
        return {'outcome': 'NO_DATA', 'reason': 'bad timestamp'}

    # Pull daily bars first. If today's daily bar isn't published yet
    # (session still open or just closed), fall back to 5-min intraday bars
    # so we capture same-day outcomes instead of sitting on NO_DATA until tomorrow.
    df = fetch_alpaca_bars(ticker, '1Day',
        emit_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))
    intraday_fallback = False
    if df is None or df.empty:
        df = fetch_alpaca_bars(ticker, '5Min',
            emit_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))
        intraday_fallback = True
    if df is None or df.empty:
        return {'outcome': 'NO_DATA', 'reason': 'no bars returned (daily or 5min)'}

    # Walk forward and classify — direction-aware
    direction = scout.get('direction', 'CALL')
    peak = entry_ref; trough = entry_ref
    hit_target = False; hit_stop = False; hit_date = None
    for ts, row in df.iterrows():
        high = float(row['High']); low = float(row['Low'])
        peak = max(peak, high); trough = min(trough, low)
        if direction == 'PUT':
            # PUT: target is LOW breakdown (price < target), stop is HIGH invalidation (price > stop)
            if low <= target:
                hit_target = True; hit_date = ts.strftime('%Y-%m-%d'); break
            if high >= stop:
                hit_stop = True; hit_date = ts.strftime('%Y-%m-%d'); break
        else:
            # CALL: target is HIGH breakout (price > target), stop is LOW (price < stop)
            if high >= target:
                hit_target = True; hit_date = ts.strftime('%Y-%m-%d'); break
            if low <= stop:
                hit_stop = True; hit_date = ts.strftime('%Y-%m-%d'); break

    last_close = float(df['Close'].iloc[-1])
    days_elapsed = len(df) if not intraday_fallback else 0  # intraday → no full days yet
    outcome = 'HIT_TARGET' if hit_target else ('HIT_STOP' if hit_stop else 'OPEN')

    return {
        'outcome': outcome,
        'hit_date': hit_date,
        'days_elapsed': days_elapsed,
        'bar_source': 'daily' if not intraday_fallback else '5min_intraday',
        'last_close': round(last_close, 2),
        'stock_pct_move': round((last_close - entry_ref) / entry_ref * 100, 2),
        'peak_pct': round((peak - entry_ref) / entry_ref * 100, 2),
        'trough_pct': round((trough - entry_ref) / entry_ref * 100, 2),
        'target_pct_away': round((target - entry_ref) / entry_ref * 100, 2),
        'stop_pct_away': round((stop - entry_ref) / entry_ref * 100, 2),
    }


def evaluate_option_outcome(scout, positions):
    """If this scout became a paper position, return its current option P&L."""
    ticker = scout.get('ticker')
    direction = scout.get('direction')
    emit_date = (scout.get('generated_utc', '') or '')[:10]

    # Match by ticker + direction + ACCUMULATION signal_source + same emit date
    for p in positions:
        if p.get('ticker') != ticker: continue
        if p.get('direction') != direction: continue
        if p.get('signal_source') != 'ACCUMULATION': continue
        entry_time = (p.get('entry_time_utc', '') or '')[:10]
        if not entry_time: continue
        if entry_time != emit_date:
            try:
                if abs((datetime.strptime(entry_time, '%Y-%m-%d')
                        - datetime.strptime(emit_date, '%Y-%m-%d')).days) > 1:
                    continue
            except Exception:
                continue
        return {
            'paper_traded': True,
            'position_status': p.get('status'),
            'entry_price_filled': p.get('entry_price_filled'),
            'qty': p.get('qty'),
            'current_premium': p.get('current_premium'),
            'unrealized_pnl_pct': p.get('unrealized_pnl_pct'),
            'realized_pnl_usd': p.get('realized_pnl_usd'),
            'realized_pnl_pct': p.get('realized_pnl_pct'),
            'exit_reason': p.get('exit_reason'),
        }
    return {'paper_traded': False}


def main():
    print("\n" + "="*82)
    print("V4 ACCUMULATION SUCCESS TRACKER (Strategy B1)")
    print("="*82)

    scouts = collect_all_scouts()
    print(f"\nTotal unique scouts (current + archived): {len(scouts)}")

    positions = []
    if os.path.exists(POSITIONS_PATH):
        with open(POSITIONS_PATH) as f: positions = json.load(f)

    # Evaluate each — enrich with theme + pattern features for long-term analysis
    records = []
    for s in scouts:
        stock = evaluate_stock_outcome(s)
        option = evaluate_option_outcome(s, positions)
        entry = s.get('entry_price_ref') or 0
        target = s.get('target_above') or 0
        stop = s.get('stop_below') or 0
        c_low = s.get('compression_low') or 0
        c_high = s.get('compression_high') or 0
        # Position within range at scout time (0% = at low, 100% = at high)
        pos_in_range = ((entry - c_low) / (c_high - c_low) * 100) if (c_high and c_low and c_high > c_low) else None
        # Range width as % of midpoint
        range_width_pct = ((c_high - c_low) / ((c_high + c_low) / 2) * 100) if (c_high and c_low) else None
        # Distance from entry to target
        dist_to_target_pct = ((target - entry) / entry * 100) if entry else None

        records.append({
            'ticker': s.get('ticker'),
            'theme': classify_theme(s.get('ticker')),
            'direction': s.get('direction'),
            'score': s.get('score'),
            'tier': s.get('tier'),
            'emit_utc': s.get('generated_utc'),
            'emit_date': (s.get('generated_utc', '') or '')[:10],
            'entry_price_ref': entry,
            'stop': stop,
            'target': target,
            'pos_in_range_pct': round(pos_in_range, 1) if pos_in_range is not None else None,
            'range_width_pct': round(range_width_pct, 1) if range_width_pct is not None else None,
            'dist_to_target_pct': round(dist_to_target_pct, 2) if dist_to_target_pct is not None else None,
            **stock,
            **option,
        })

    # Aggregate stats
    hit_target = [r for r in records if r.get('outcome') == 'HIT_TARGET']
    hit_stop = [r for r in records if r.get('outcome') == 'HIT_STOP']
    open_still = [r for r in records if r.get('outcome') == 'OPEN']
    no_data = [r for r in records if r.get('outcome') == 'NO_DATA']

    paper_traded = [r for r in records if r.get('paper_traded')]
    realized_pnl = sum((r.get('realized_pnl_usd') or 0) for r in paper_traded if r.get('realized_pnl_usd') is not None)
    open_unrealized_pct = [r.get('unrealized_pnl_pct', 0) for r in paper_traded
                           if r.get('position_status') == 'OPEN' and r.get('unrealized_pnl_pct') is not None]
    avg_open_unrl = sum(open_unrealized_pct) / len(open_unrealized_pct) if open_unrealized_pct else 0

    resolved = len(hit_target) + len(hit_stop)
    win_rate = len(hit_target) / resolved if resolved else 0

    print(f"\n=== Stock-level outcomes ===")
    print(f"  HIT TARGET:  {len(hit_target)}  (reached compression_high)")
    print(f"  HIT STOP:    {len(hit_stop)}    (breakdown below compression_low-1%)")
    print(f"  OPEN:        {len(open_still)}")
    print(f"  NO_DATA:     {len(no_data)}")
    if resolved > 0:
        print(f"  Resolved win rate: {win_rate*100:.1f}% ({len(hit_target)}W / {len(hit_stop)}L)")

    print(f"\n=== Paper-traded scouts (actual option P&L) ===")
    print(f"  Positions opened: {len(paper_traded)}")
    closed_pt = [r for r in paper_traded if r.get('position_status') == 'CLOSED']
    open_pt = [r for r in paper_traded if r.get('position_status') == 'OPEN']
    print(f"  Closed:  {len(closed_pt)}  realized P&L ${realized_pnl:+.0f}")
    print(f"  Open:    {len(open_pt)}   avg unrealized {avg_open_unrl:+.1f}%")

    print(f"\n=== Top 10 scouts by stock pct_move ===")
    print(f"  {'ticker':<7} {'dir':<5} {'emit_date':<12} {'outcome':<12} {'move%':>7} {'peak%':>7} {'paper':<6}")
    sorted_by_move = sorted(records, key=lambda x: -(x.get('stock_pct_move') or 0))
    for r in sorted_by_move[:10]:
        traded = 'Y' if r.get('paper_traded') else '-'
        print(f"  {r['ticker']:<7} {r['direction']:<5} {r['emit_date']:<12} {r['outcome']:<12} "
              f"{r.get('stock_pct_move',0):>+6.1f}% {r.get('peak_pct',0):>+6.1f}% {traded:<6}")

    payload = {
        'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'summary': {
            'total_scouts': len(records),
            'hit_target': len(hit_target),
            'hit_stop': len(hit_stop),
            'open': len(open_still),
            'no_data': len(no_data),
            'resolved_win_rate': round(win_rate, 3),
            'paper_positions_opened': len(paper_traded),
            'paper_closed': len(closed_pt),
            'paper_open': len(open_pt),
            'realized_pnl_usd': round(realized_pnl, 2),
            'avg_open_unrealized_pct': round(avg_open_unrl, 2),
        },
        'records': records,
    }
    atomic_write(OUT_PATH, payload)
    print(f"\n💾 Saved: {OUT_PATH}")

    # Append one line per scout to the append-only history log. Dedupe: only
    # log if we haven't logged this (ticker, emit_date) pair already.
    try:
        seen = set()
        if os.path.exists(OUTCOMES_HISTORY):
            with open(OUTCOMES_HISTORY) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        seen.add((e.get('ticker'), e.get('emit_date'), e.get('outcome')))
                    except Exception: continue

        now_iso = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        appended = 0
        with open(OUTCOMES_HISTORY, 'a') as f:
            for r in records:
                key = (r.get('ticker'), r.get('emit_date'), r.get('outcome'))
                if key in seen: continue
                # Only archive rows that have resolved (hit target/stop) OR that
                # have stock_pct_move populated (so we have SOMETHING to analyze later).
                if r.get('outcome') in ('NO_DATA',) and r.get('stock_pct_move') in (None, 0):
                    continue
                entry = {'logged_utc': now_iso, **r}
                f.write(json.dumps(entry, default=str) + '\n')
                appended += 1
        if appended > 0:
            print(f"📜 Appended {appended} new rows to {OUTCOMES_HISTORY}")
    except Exception as e:
        print(f"⚠️  Could not append to outcomes history: {e}")


def archive_current_signals():
    """Copy v4_accumulation_signals.json → archive/YYYY-MM-DD.json if date differs.
    Keeps history so tomorrow's scan doesn't erase today's scouts."""
    if not os.path.exists(ACC_SIGNALS_PATH): return
    try:
        with open(ACC_SIGNALS_PATH) as f: data = json.load(f)
        emit_date = (data.get('metadata', {}).get('generated_utc', '') or '')[:10]
        if not emit_date: return
        os.makedirs(ACC_ARCHIVE_DIR, exist_ok=True)
        archive_path = os.path.join(ACC_ARCHIVE_DIR, f'{emit_date}.json')
        # Only write archive if file doesn't exist yet for this date
        if not os.path.exists(archive_path):
            with open(archive_path, 'w') as f: json.dump(data, f, indent=2, default=str)
    except Exception: pass


if __name__ == "__main__":
    archive_current_signals()
    main()
