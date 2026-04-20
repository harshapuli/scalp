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

    # Pull daily bars from emit day through now
    df = fetch_alpaca_bars(ticker, '1Day',
        emit_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty:
        return {'outcome': 'NO_DATA', 'reason': 'no bars returned'}

    # Walk forward and classify
    peak = entry_ref; trough = entry_ref
    hit_target = False; hit_stop = False; hit_date = None
    for ts, row in df.iterrows():
        high = float(row['High']); low = float(row['Low'])
        peak = max(peak, high); trough = min(trough, low)
        # For CALL direction, target is up and stop is down
        if high >= target:
            hit_target = True; hit_date = ts.strftime('%Y-%m-%d'); break
        if low <= stop:
            hit_stop = True; hit_date = ts.strftime('%Y-%m-%d'); break

    last_close = float(df['Close'].iloc[-1])
    days_elapsed = len(df)
    outcome = 'HIT_TARGET' if hit_target else ('HIT_STOP' if hit_stop else 'OPEN')

    return {
        'outcome': outcome,
        'hit_date': hit_date,
        'days_elapsed': days_elapsed,
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

    # Evaluate each
    records = []
    for s in scouts:
        stock = evaluate_stock_outcome(s)
        option = evaluate_option_outcome(s, positions)
        records.append({
            'ticker': s.get('ticker'),
            'direction': s.get('direction'),
            'score': s.get('score'),
            'tier': s.get('tier'),
            'emit_utc': s.get('generated_utc'),
            'emit_date': (s.get('generated_utc', '') or '')[:10],
            'entry_price_ref': s.get('entry_price_ref'),
            'stop': s.get('stop_below'),
            'target': s.get('target_above'),
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
