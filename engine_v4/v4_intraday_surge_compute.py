"""
V4 INTRADAY SURGE COMPUTE — find max 30-min surge anywhere in today's session.

User insight: "institutions distribute early, accumulate late — we need
to catch both."

Backtest evidence (60d, n=2700):
  - Opening-hour PUT surges (institutional selling) → 60% 3d-down win
    rate at ≥-$1M (n=70). Our EOD chip catches only 16 of these.
  - Opening-hour CALL surges → 31% 3d-up (worse than baseline) — noise.

So the build is asymmetric: scan intraday-anywhere for PUT, keep
end-of-day-only for CALL.

This script:
  1. Reads the latest v4_snapshot_<date>.json (has all of today's ticks)
  2. For each picks_77 ticker, slides a 30-min rolling window across
     today's net_prem_ticks
  3. Records max-CALL window + max-PUT window per ticker (with
     timestamps so we know WHEN it happened)
  4. Writes data/intraday_surge_state.json — read by BucketView

Schedule: every 5 min during market hours (cheap — reads snapshot file).
Output: data/intraday_surge_state.json
"""
import os, sys, json, glob
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data', 'intraday_surge_state.json')
PICKS = os.path.join(HERE, 'data', 'picks_77.json')

WINDOW_MIN = 30
PUT_ALERT_THRESHOLD = -1.0   # $M — PUT chip fires below this
CALL_ALERT_THRESHOLD = 1.0   # $M — CALL chip fires above this


def latest_snapshot_path():
    paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    return paths[-1] if paths else None


def load_picks():
    if not os.path.exists(PICKS): return set()
    d = json.load(open(PICKS))
    s = set()
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (d.get('buckets') or {}).get(k, []) or []:
            s.add(r['ticker'])
    return s


def best_windows(ticks):
    """For one ticker's intraday tick stream, return:
       max_call_m, max_call_t, max_put_m, max_put_t
       (max_call >= 0; max_put <= 0)
    """
    if not isinstance(ticks, list) or len(ticks) < WINDOW_MIN:
        return None, None, None, None
    sorted_ticks = sorted(ticks, key=lambda t: (t.get('tape_time') or t.get('t') or ''))
    best_call = best_put = 0.0
    best_call_t = best_put_t = None
    for i in range(len(sorted_ticks) - WINDOW_MIN + 1):
        s = 0.0
        for t in sorted_ticks[i:i + WINDOW_MIN]:
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
        # Window's start time
        t_iso = sorted_ticks[i].get('tape_time') or sorted_ticks[i].get('t')
        if s > best_call:
            best_call = s
            best_call_t = t_iso
        if s < best_put:
            best_put = s
            best_put_t = t_iso
    return best_call / 1e6, best_call_t, best_put / 1e6, best_put_t


def main():
    snap_path = latest_snapshot_path()
    if not snap_path:
        print('no snapshot file', file=sys.stderr); return 1
    snap = json.load(open(snap_path)).get('data') or {}
    picks = load_picks()

    out_per_ticker = {}
    n_call_alerts = n_put_alerts = 0

    for tkr in picks:
        blob = snap.get(tkr)
        if not blob: continue
        npt = blob.get('net_prem_ticks')
        if isinstance(npt, dict): npt = npt.get('data') or []
        max_call_m, max_call_t, max_put_m, max_put_t = best_windows(npt)
        if max_call_m is None: continue

        rec = {
            'ticker':            tkr,
            'max_call_m':        round(max_call_m, 3),
            'max_call_window_t': max_call_t,
            'max_put_m':         round(max_put_m, 3),
            'max_put_window_t':  max_put_t,
        }
        # Fire flags — actionable thresholds
        if max_call_m >= CALL_ALERT_THRESHOLD:
            rec['call_alert'] = True
            n_call_alerts += 1
        if max_put_m <= PUT_ALERT_THRESHOLD:
            rec['put_alert'] = True
            n_put_alerts += 1
        out_per_ticker[tkr] = rec

    out = {
        'generated_utc':       datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'snapshot_used':       os.path.basename(snap_path),
        'window_size_min':     WINDOW_MIN,
        'call_alert_threshold_m': CALL_ALERT_THRESHOLD,
        'put_alert_threshold_m':  PUT_ALERT_THRESHOLD,
        'n_tickers_scanned':   len(out_per_ticker),
        'n_call_alerts':       n_call_alerts,
        'n_put_alerts':        n_put_alerts,
        'tickers':             out_per_ticker,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f'Scanned {len(out_per_ticker)} tickers from {os.path.basename(snap_path)}')
    print(f'  CALL alerts (max_call ≥ +${CALL_ALERT_THRESHOLD}M): {n_call_alerts}')
    print(f'  PUT alerts  (max_put  ≤ -${abs(PUT_ALERT_THRESHOLD)}M): {n_put_alerts}')

    # Show top movers
    sorted_call = sorted(out_per_ticker.values(), key=lambda r: -r['max_call_m'])[:10]
    sorted_put = sorted(out_per_ticker.values(), key=lambda r: r['max_put_m'])[:10]
    print(f'\nTop 10 CALL surges (anywhere in day):')
    for r in sorted_call:
        t = (r['max_call_window_t'] or '')[11:16]
        print(f"  {r['ticker']:<6} +${r['max_call_m']:>6.2f}M  window {t} UTC")
    print(f'\nTop 10 PUT surges (anywhere in day):')
    for r in sorted_put:
        t = (r['max_put_window_t'] or '')[11:16]
        print(f"  {r['ticker']:<6} -${abs(r['max_put_m']):>6.2f}M  window {t} UTC")

    print(f'\nSaved → {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
