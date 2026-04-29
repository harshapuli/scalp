"""
V4 EOD SURGE LOGGER — persists late-hour flow surges from snapshot tick data.

Runs after market close. For each ticker in data/picks_77.json:
  1. Read v4_snapshot_<today>.json's net_prem_ticks (per-minute UW flow)
  2. Sum net_call_premium for the LAST 30 MIN window (19:30-20:00 UTC)
     — same metric as v4_late_hour_flow_backtest.py
  3. If the sum crosses ±$5M (SURGE) or ±$1M (STRONG), append to
     data/eod_surge_log.jsonl with the day's outcome (window return,
     next-day gap if available).

Why this design (vs reading live patrol):
  - Patrol's last_hr_call_$ field decays after the window closes.
  - Snapshot's net_prem_ticks is preserved — gives us deterministic,
    reproducible backtest-grade data.
  - Schedule: ONCE per day post-close (~1:10 PT). Cheap.

The log accumulates over time. After ~30 days we'll have meaningful
sample size to validate the +30.7pp edge holds out-of-window.

Run: python3 v4_eod_surge_logger.py [--date YYYY-MM-DD]
Output: data/eod_surge_log.jsonl
"""
import os, sys, json, glob
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PICKS_FILE = os.path.join(HERE, 'data', 'picks_77.json')
LOG_FILE   = os.path.join(HERE, 'data', 'eod_surge_log.jsonl')

CALL_SURGE_USD  = 5_000_000   # SURGE_5M+ tier (45.5% backtest win)
CALL_STRONG_USD = 1_000_000   # STRONG_1-5M tier (47.6% next-day gap-up)

# Window: 19:30-20:00 UTC = 12:30-1:00 PT = last 30 min RTH
WINDOW_START_UTC = '19:30'
WINDOW_END_UTC   = '20:00'


def load_picks_tickers():
    if not os.path.exists(PICKS_FILE): return set()
    d = json.load(open(PICKS_FILE))
    s = set()
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (d.get('buckets') or {}).get(k, []) or []:
            s.add(r.get('ticker'))
    return s


def load_already_logged_today(today_iso):
    seen = set()
    if not os.path.exists(LOG_FILE): return seen
    try:
        with open(LOG_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get('session_date') == today_iso:
                        seen.add(e.get('ticker'))
                except: pass
    except: pass
    return seen


def compute_late_window_call(blob):
    """Return (last_30m_net_call_$_total, last_30m_window_return_pct, day_close)."""
    npt = blob.get('net_prem_ticks')
    if isinstance(npt, dict): npt = npt.get('data') or []
    if not isinstance(npt, list) or not npt: return None, None, None

    window_call = 0.0
    ticks_in_window = 0
    for t in npt:
        tape_t = t.get('tape_time') or t.get('t') or ''
        hh_mm = tape_t[11:16]
        if WINDOW_START_UTC <= hh_mm <= WINDOW_END_UTC:
            ticks_in_window += 1
            try: window_call += float(t.get('net_call_premium', 0) or 0)
            except: pass
    if ticks_in_window == 0: return None, None, None

    # Window return + day close from 5-min bars
    bars5 = blob.get('alpaca_bars_5m')
    if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
    start_px, end_px, close_px = None, None, None
    for b in (bars5 or []):
        hh_mm = (b.get('t') or '')[11:16]
        if hh_mm == WINDOW_START_UTC:
            start_px = b.get('o') or b.get('c')
        if WINDOW_START_UTC <= hh_mm <= WINDOW_END_UTC:
            end_px = b.get('c')
        close_px = b.get('c')
    window_return = None
    if start_px and end_px and start_px > 0:
        try: window_return = (float(end_px) - float(start_px)) / float(start_px) * 100
        except: pass
    return window_call, window_return, close_px


def find_snapshot(date_iso):
    p = os.path.join(HERE, f'v4_snapshot_{date_iso}.json')
    return p if os.path.exists(p) else None


def main():
    date_arg = None
    if '--date' in sys.argv:
        i = sys.argv.index('--date')
        if i + 1 < len(sys.argv): date_arg = sys.argv[i + 1]

    if date_arg:
        snap_path = find_snapshot(date_arg)
        if not snap_path:
            print(f'no snapshot for {date_arg}', file=sys.stderr); return 1
        today_iso = date_arg
    else:
        # Use latest snapshot (ignores date_arg)
        candidates = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
        if not candidates:
            print('no snapshot files', file=sys.stderr); return 1
        snap_path = candidates[-1]
        today_iso = os.path.basename(snap_path).replace('v4_snapshot_', '').replace('.json', '')

    snap = json.load(open(snap_path)).get('data') or {}
    picks = load_picks_tickers()
    already = load_already_logged_today(today_iso)

    print(f'Logging late-hour surges for {today_iso} from {os.path.basename(snap_path)}...', file=sys.stderr)
    print(f'  picks_77 tickers: {len(picks)}', file=sys.stderr)
    print(f'  already logged today: {len(already)}', file=sys.stderr)

    fires = []
    for tkr in picks:
        if tkr in already: continue
        blob = snap.get(tkr)
        if not blob: continue
        window_call, window_ret, close_px = compute_late_window_call(blob)
        if window_call is None: continue

        last_30m_m = window_call / 1e6
        signal = None
        tier = None
        if window_call >= CALL_SURGE_USD:
            signal, tier = 'CALL_SURGE', 'SURGE_5M+'
        elif window_call >= CALL_STRONG_USD:
            signal, tier = 'CALL_STRONG', 'STRONG_1-5M'
        elif window_call <= -CALL_SURGE_USD:
            signal, tier = 'PUT_SURGE', 'PUT_5M+'
        elif window_call <= -CALL_STRONG_USD:
            signal, tier = 'PUT_STRONG', 'PUT_1-5M'
        else:
            continue

        fires.append({
            'ticker':           tkr,
            'session_date':     today_iso,
            'logged_at_utc':    datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'signal_type':      signal,
            'tier':             tier,
            'last_30m_call_m':  round(last_30m_m, 3),
            'window_return_pct': round(window_ret, 3) if window_ret is not None else None,
            'close_price':      close_px,
            # Note: next-day gap will be computed during weekly review
            # by scanning forward to the next session's snapshot.
        })

    if not fires:
        print(f'no new surges to log for {today_iso}')
        return 0

    fires.sort(key=lambda r: -abs(r['last_30m_call_m']))

    with open(LOG_FILE, 'a') as f:
        for r in fires:
            f.write(json.dumps(r, default=str) + '\n')

    print(f'\n=== {today_iso} — {len(fires)} surge(s) logged ===')
    for r in fires:
        side = 'CALL' if 'CALL' in r['signal_type'] else 'PUT'
        emoji = '🎯' if r['tier'].endswith('5M+') else '📞'
        ret = f"{r['window_return_pct']:+.2f}%" if r['window_return_pct'] is not None else '—'
        print(f"  {emoji} {r['ticker']:<6} {r['signal_type']:<12} ${r['last_30m_call_m']:+7.2f}M  "
              f"window_return: {ret:>7}")
    print(f'\nappended → {LOG_FILE}')
    print(f'total events in log: {sum(1 for _ in open(LOG_FILE))}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
