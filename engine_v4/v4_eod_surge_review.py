"""
V4 EOD SURGE REVIEW — backtest the persisted late-hour surge log.

Reads data/eod_surge_log.jsonl (73 events backfilled from Apr 22-29),
joins each event with the next-day open from snapshot files, and
computes hit rates per signal type.

Tests for each event:
  1. Window return (last 30 min of fire-day) — already logged
  2. Next-day open gap (vs fire-day close)
  3. Next-day close vs fire-day close (1d hold return)
  4. Day-3 close vs fire-day close (3d hold return)

Reports:
  - Win rates per signal type (CALL_SURGE / CALL_STRONG / PUT_SURGE / PUT_STRONG)
  - Per-ticker performance
  - Best & worst signals
  - Failure patterns

Run: python3 v4_eod_surge_review.py
Output: v4_eod_surge_review.json + console table
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, 'data', 'eod_surge_log.jsonl')
OUT = os.path.join(HERE, 'v4_eod_surge_review.json')

WIN_THRESHOLD = 0.5  # % move that counts as a hit


def load_log():
    if not os.path.exists(LOG_FILE): return []
    events = []
    with open(LOG_FILE) as f:
        for line in f:
            try: events.append(json.loads(line))
            except: pass
    return events


def load_snapshots():
    snaps = {}
    for p in sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json'))):
        d = os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    return snaps


def get_open_close(snap_data, ticker):
    """Return (open_price, close_price) from a snapshot's dailyBar.
    open = first 13:30Z bar from alpaca_bars_5m
    close = dailyBar.c
    """
    blob = snap_data.get(ticker) or {}
    db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
    close = db.get('c')
    bars5 = blob.get('alpaca_bars_5m')
    if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
    open_px = None
    for b in (bars5 or []):
        if (b.get('t') or '')[11:16] == '13:30':
            open_px = b.get('o') or b.get('c')
            break
    try:
        return float(open_px) if open_px is not None else None, float(close) if close is not None else None
    except:
        return None, None


def main():
    events = load_log()
    if not events:
        print('no events in log', file=sys.stderr); return 1
    snaps = load_snapshots()
    dates = sorted(snaps.keys())

    # Enrich each event with forward-looking outcomes
    enriched = []
    for e in events:
        d = e.get('session_date')
        tkr = e.get('ticker')
        if d not in dates: continue
        idx = dates.index(d)

        fire_close = e.get('close_price')
        # Next-day open + close
        next_open, next_close = None, None
        next_d = dates[idx + 1] if idx + 1 < len(dates) else None
        if next_d:
            next_open, next_close = get_open_close(snaps[next_d], tkr)
        # Day-3 close
        day3_close = None
        d3_idx = idx + 3
        if d3_idx < len(dates):
            _, day3_close = get_open_close(snaps[dates[d3_idx]], tkr)

        gap_pct = None
        next_close_pct = None
        day3_pct = None
        if fire_close and next_open is not None and fire_close > 0:
            try: gap_pct = (next_open - float(fire_close)) / float(fire_close) * 100
            except: pass
        if fire_close and next_close is not None and fire_close > 0:
            try: next_close_pct = (next_close - float(fire_close)) / float(fire_close) * 100
            except: pass
        if fire_close and day3_close is not None and fire_close > 0:
            try: day3_pct = (day3_close - float(fire_close)) / float(fire_close) * 100
            except: pass

        enriched.append({
            **e,
            'next_open': next_open,
            'next_close': next_close,
            'day3_close': day3_close,
            'gap_pct': round(gap_pct, 3) if gap_pct is not None else None,
            'next_1d_close_pct': round(next_close_pct, 3) if next_close_pct is not None else None,
            'day3_close_pct': round(day3_pct, 3) if day3_pct is not None else None,
        })

    # Aggregate by signal_type
    def is_bull(sig): return sig.startswith('CALL_')
    def aggregate(group, side='bull'):
        if not group: return {'n': 0}
        # Direction: bull → up = win; bear → down = win
        sign = 1 if side == 'bull' else -1
        results = {'n': len(group), 'tier': group[0].get('tier')}
        for metric, key in [('window', 'window_return_pct'),
                            ('gap',    'gap_pct'),
                            ('next_close', 'next_1d_close_pct'),
                            ('day3', 'day3_close_pct')]:
            vals = [r[key] for r in group if r.get(key) is not None]
            if not vals:
                results[f'{metric}_n'] = 0
                continue
            wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
            results[f'{metric}_n'] = len(vals)
            results[f'{metric}_win_rate'] = round(wins / len(vals) * 100, 1)
            results[f'{metric}_avg_pct']  = round(statistics.mean(vals), 3)
            results[f'{metric}_avg_aligned'] = round(statistics.mean(v * sign for v in vals), 3)
        return results

    by_sig = defaultdict(list)
    for r in enriched:
        by_sig[r['signal_type']].append(r)
    summary = {}
    for sig, group in by_sig.items():
        side = 'bull' if is_bull(sig) else 'bear'
        summary[sig] = aggregate(group, side)

    # Best and worst events (signal-aligned: how much it moved in the direction we predicted)
    def aligned_score(r):
        sign = 1 if is_bull(r['signal_type']) else -1
        m = r.get('day3_close_pct') or r.get('next_1d_close_pct') or r.get('gap_pct') or r.get('window_return_pct')
        return (m or 0) * sign
    best = sorted(enriched, key=lambda r: -aligned_score(r))[:10]
    worst = sorted(enriched, key=lambda r: aligned_score(r))[:10]

    # Per-ticker summary
    by_tkr = defaultdict(list)
    for r in enriched:
        by_tkr[r['ticker']].append(r)
    tkr_summary = []
    for tkr, group in by_tkr.items():
        tkr_summary.append({
            'ticker': tkr,
            'n_fires': len(group),
            'avg_aligned_3d': round(statistics.mean(aligned_score(r) for r in group), 2),
            'signals': sorted(set(r['signal_type'] for r in group)),
        })
    tkr_summary.sort(key=lambda x: -x['avg_aligned_3d'])

    out = {
        'generated_utc':    datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'log_file':         LOG_FILE,
        'total_events':     len(events),
        'enriched_events':  len(enriched),
        'win_threshold_pct': WIN_THRESHOLD,
        'sessions_used':    dates,
        'by_signal_type':   summary,
        'best_10':          best,
        'worst_10':         worst,
        'per_ticker':       tkr_summary,
        'all_events':       enriched,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console
    print(f"\n{'='*108}")
    print(f"V4 EOD SURGE REVIEW — backtest of persisted log")
    print(f"{len(enriched)}/{len(events)} events with forward data  ·  win threshold ±{WIN_THRESHOLD}%")
    print(f"sessions: {dates[0]} → {dates[-1]}")
    print(f"{'='*108}\n")
    print(f"{'signal':<14} {'n':>4} | {'window':>8} {'win%':>6} | {'gap':>7} {'win%':>6} | {'1d_close':>9} {'win%':>6} | {'3d':>7} {'win%':>6}")
    print('-' * 108)
    for sig in sorted(summary.keys()):
        s = summary[sig]
        def fmt(metric):
            avg = s.get(f'{metric}_avg_aligned')
            wr  = s.get(f'{metric}_win_rate')
            n   = s.get(f'{metric}_n', 0)
            avg_s = f"{avg:+.2f}%" if avg is not None else f'(n={n})'
            wr_s  = f"{wr}%" if wr is not None else '—'
            return avg_s, wr_s
        w_avg, w_wr = fmt('window')
        g_avg, g_wr = fmt('gap')
        c_avg, c_wr = fmt('next_close')
        d3_avg, d3_wr = fmt('day3')
        print(f"{sig:<14} {s['n']:>4} | {w_avg:>8} {w_wr:>6} | {g_avg:>7} {g_wr:>6} | {c_avg:>9} {c_wr:>6} | {d3_avg:>7} {d3_wr:>6}")

    print(f"\n=== TOP 10 BEST EVENTS (3d-aligned move in predicted direction) ===")
    print(f"{'tkr':<6} {'date':<11} {'signal':<13} {'late_$M':>8} {'window':>7} {'gap':>7} {'1d_cls':>7} {'3d':>7}")
    for r in best:
        d3  = f"{r['day3_close_pct']:+.2f}" if r.get('day3_close_pct') is not None else '—'
        nc  = f"{r['next_1d_close_pct']:+.2f}" if r.get('next_1d_close_pct') is not None else '—'
        gap = f"{r['gap_pct']:+.2f}" if r.get('gap_pct') is not None else '—'
        wnd = f"{r['window_return_pct']:+.2f}" if r.get('window_return_pct') is not None else '—'
        print(f"{r['ticker']:<6} {r['session_date']:<11} {r['signal_type']:<13} {r['last_30m_call_m']:>+7.1f}M {wnd:>6} {gap:>6} {nc:>6} {d3:>6}")

    print(f"\n=== BOTTOM 10 WORST EVENTS (moved AGAINST the signal) ===")
    print(f"{'tkr':<6} {'date':<11} {'signal':<13} {'late_$M':>8} {'window':>7} {'gap':>7} {'1d_cls':>7} {'3d':>7}")
    for r in worst:
        d3  = f"{r['day3_close_pct']:+.2f}" if r.get('day3_close_pct') is not None else '—'
        nc  = f"{r['next_1d_close_pct']:+.2f}" if r.get('next_1d_close_pct') is not None else '—'
        gap = f"{r['gap_pct']:+.2f}" if r.get('gap_pct') is not None else '—'
        wnd = f"{r['window_return_pct']:+.2f}" if r.get('window_return_pct') is not None else '—'
        print(f"{r['ticker']:<6} {r['session_date']:<11} {r['signal_type']:<13} {r['last_30m_call_m']:>+7.1f}M {wnd:>6} {gap:>6} {nc:>6} {d3:>6}")

    print(f"\n=== PER-TICKER (avg aligned 3d move, sorted best→worst) ===")
    print(f"{'tkr':<6} {'fires':>5} {'avg_3d':>8}  signals fired")
    for r in tkr_summary[:30]:
        print(f"{r['ticker']:<6} {r['n_fires']:>5} {r['avg_aligned_3d']:>+7.2f}%  {','.join(r['signals'])}")

    print(f"\nSaved → {OUT}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
