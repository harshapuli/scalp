"""
V4 EOD SURGE REGIME SWEEP — split by earnings-ahead vs not.

User insight: earnings is the catalyst that JUSTIFIES the volume.
A stock with earnings in 5 days + late-hour call surge = institutions
positioning ahead of a known binary. A stock with no earnings + same
surge = stealth positioning ahead of an unknown move. These are
DIFFERENT signals, and the optimal threshold likely differs.

Splits ticker-days into 3 regimes:
  EARN_WK   — earnings within ±7 days (pre/post-event positioning)
  EARN_MTH  — earnings 8-30 days out (medium-horizon positioning)
  NO_EARN   — no earnings inside 30 days (organic flow only)

For each regime, sweeps thresholds and reports edge vs baseline (within
that regime). Lets us answer: "what threshold should we alert at, given
whether the stock has earnings ahead?"

Run: python3 v4_eod_surge_regime_sweep.py
Output: v4_eod_surge_regime_sweep.json + console
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5


def date_from_snap(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def get_open_close(snap_data, tkr):
    blob = snap_data.get(tkr) or {}
    db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
    close = db.get('c')
    bars5 = blob.get('alpaca_bars_5m')
    if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
    open_px = None
    for b in (bars5 or []):
        if (b.get('t') or '')[11:16] == '13:30':
            open_px = b.get('o') or b.get('c'); break
    try:
        return (float(open_px) if open_px is not None else None,
                float(close) if close is not None else None)
    except: return None, None


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from_snap(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())

    # earnings dates
    latest = json.load(open(snap_paths[-1])).get('data') or {}
    earnings_by_t = {}
    from datetime import datetime as _dt
    for tkr, blob in latest.items():
        info = (blob.get('info', {}).get('data') or {})
        e = info.get('next_earnings_date')
        if e:
            try: earnings_by_t[tkr] = _dt.strptime(e, '%Y-%m-%d').date()
            except: pass

    # picks
    picks_path = os.path.join(HERE, 'data', 'picks_77.json')
    picks = set()
    if os.path.exists(picks_path):
        pd = json.load(open(picks_path))
        for k in ('mega', 'mid', 'small', 'indices'):
            for r in (pd.get('buckets') or {}).get(k, []) or []: picks.add(r['ticker'])

    def regime_for(tkr, session_iso):
        e = earnings_by_t.get(tkr)
        if not e: return 'NO_EARN'
        s = _dt.strptime(session_iso, '%Y-%m-%d').date()
        delta = abs((e - s).days)
        if delta <= 7:  return 'EARN_WK'
        if delta <= 30: return 'EARN_MTH'
        return 'NO_EARN'

    obs = []
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if picks and tkr not in picks: continue

            # last_30m_call_$
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue
            wcall = 0.0
            for t in npt:
                hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                if '19:30' <= hh_mm <= '20:00':
                    try: wcall += float(t.get('net_call_premium', 0) or 0)
                    except: pass
            wcall_m = wcall / 1e6

            # window/close prices
            bars5 = blob.get('alpaca_bars_5m')
            if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
            start_px, end_px, close_px = None, None, None
            for b in (bars5 or []):
                hh_mm = (b.get('t') or '')[11:16]
                if hh_mm == '19:30': start_px = b.get('o') or b.get('c')
                if '19:30' <= hh_mm <= '20:00': end_px = b.get('c')
                close_px = b.get('c')
            window_ret = None
            if start_px and end_px and start_px > 0:
                try: window_ret = (float(end_px) - float(start_px)) / float(start_px) * 100
                except: pass

            # forward
            day3_close = None
            if i + 3 < len(dates):
                _, day3_close = get_open_close(snaps[dates[i + 3]], tkr)
            day3_pct = None
            if close_px and day3_close is not None and float(close_px) > 0:
                day3_pct = (day3_close - float(close_px)) / float(close_px) * 100

            # next-1d
            n_close = None
            if i + 1 < len(dates):
                _, n_close = get_open_close(snaps[dates[i + 1]], tkr)
            next_1d_pct = None
            if close_px and n_close is not None and float(close_px) > 0:
                next_1d_pct = (n_close - float(close_px)) / float(close_px) * 100

            obs.append({
                'ticker': tkr, 'date': d,
                'regime': regime_for(tkr, d),
                'days_to_earnings': abs((earnings_by_t[tkr] - _dt.strptime(d, '%Y-%m-%d').date()).days)
                                    if tkr in earnings_by_t else None,
                'call_m': round(wcall_m, 3),
                'window_ret':     round(window_ret, 3) if window_ret is not None else None,
                'next_1d_pct':    round(next_1d_pct, 3) if next_1d_pct is not None else None,
                'day3_pct':       round(day3_pct, 3) if day3_pct is not None else None,
            })

    print(f'\nobs by regime:', file=sys.stderr)
    for r in ('EARN_WK', 'EARN_MTH', 'NO_EARN'):
        count = sum(1 for o in obs if o['regime'] == r)
        print(f'  {r}: {count}', file=sys.stderr)

    # ── stats helper ──
    def stats_for(group, side, metric_key):
        sign = 1 if side == 'call' else -1
        vals = [r[metric_key] for r in group if r.get(metric_key) is not None]
        if not vals: return {'n': 0}
        wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
        return {
            'n': len(vals),
            'win_pct': round(wins / len(vals) * 100, 1),
            'avg_aligned': round(statistics.mean(v * sign for v in vals), 3),
        }

    # baselines per regime per side per horizon
    def baselines(regime):
        group = [o for o in obs if o['regime'] == regime]
        return {
            'call_3d':   stats_for(group, 'call', 'day3_pct'),
            'put_3d':    stats_for(group, 'put',  'day3_pct'),
            'call_1d':   stats_for(group, 'call', 'next_1d_pct'),
            'put_1d':    stats_for(group, 'put',  'next_1d_pct'),
            'n_total':   len(group),
        }

    regime_baselines = {r: baselines(r) for r in ('EARN_WK', 'EARN_MTH', 'NO_EARN')}

    thresholds = [0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 7.5, 10]

    def sweep_regime(regime, side):
        sign = 1 if side == 'call' else -1
        rows = []
        for t in thresholds:
            cutoff = t * sign
            if side == 'call':
                group = [o for o in obs if o['regime'] == regime and o['call_m'] >= cutoff]
            else:
                group = [o for o in obs if o['regime'] == regime and o['call_m'] <= cutoff]
            s_3d = stats_for(group, side, 'day3_pct')
            s_1d = stats_for(group, side, 'next_1d_pct')
            rows.append({
                'threshold_m': cutoff,
                'n_events': len(group),
                'day3': s_3d, 'next_1d': s_1d,
            })
        return rows

    sweeps = {}
    for r in ('EARN_WK', 'EARN_MTH', 'NO_EARN'):
        sweeps[r] = {'call': sweep_regime(r, 'call'),
                     'put':  sweep_regime(r, 'put')}

    out = {
        'generated_utc':  datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'sessions':       dates,
        'total_obs':      len(obs),
        'win_threshold':  WIN_THRESHOLD,
        'regime_baselines': regime_baselines,
        'sweeps':         sweeps,
    }
    with open(os.path.join(HERE, 'v4_eod_surge_regime_sweep.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console
    print(f"\n{'='*108}")
    print(f"EOD SURGE — REGIME SWEEP  ·  n={len(obs)}  ·  sessions {dates[0]} → {dates[-1]}")
    print(f"{'='*108}\n")
    print(f"REGIME BASELINES (3d horizon):")
    print(f"  {'regime':<10} {'n_total':>8} {'call_baseline':>15} {'put_baseline':>15}")
    for r in ('EARN_WK', 'EARN_MTH', 'NO_EARN'):
        b = regime_baselines[r]
        c, p = b['call_3d'], b['put_3d']
        c_n = c.get('n', 0)
        p_n = p.get('n', 0)
        c_wr = c.get('win_pct')
        p_wr = p.get('win_pct')
        c_s = f"{c_wr}% (n={c_n})" if c_wr is not None else '—'
        p_s = f"{p_wr}% (n={p_n})" if p_wr is not None else '—'
        print(f"  {r:<10} {b['n_total']:>8} {c_s:>16} {p_s:>16}")

    for regime in ('EARN_WK', 'EARN_MTH', 'NO_EARN'):
        b = regime_baselines[regime]
        c_base = b['call_3d'].get('win_pct')
        p_base = b['put_3d'].get('win_pct')
        print(f"\n--- {regime}  (baseline call={c_base}%  put={p_base}%) ---")
        print(f"  {'thresh':>9} {'n':>4} | {'call_3d_win':>13} {'avg':>8} edge | {'put_3d_win':>13} {'avg':>8} edge")
        print('-' * 100)
        for t_idx, t in enumerate(thresholds):
            c_row = sweeps[regime]['call'][t_idx]
            p_row = sweeps[regime]['put'][t_idx]
            c_n = c_row['day3'].get('n', 0)
            p_n = p_row['day3'].get('n', 0)
            c_wr = c_row['day3'].get('win_pct')
            p_wr = p_row['day3'].get('win_pct')
            c_avg = c_row['day3'].get('avg_aligned')
            p_avg = p_row['day3'].get('avg_aligned')

            def fmt(wr, base, n, avg):
                if wr is None or base is None or n == 0:
                    return '—'.rjust(13), '—'.rjust(8), '—'.rjust(7)
                edge = wr - base
                return (f"{wr}% (n={n})".rjust(13),
                        f"{avg:+.2f}%".rjust(8) if avg is not None else '—'.rjust(8),
                        f"{edge:+.0f}pp".rjust(7))
            cwr_s, c_avg_s, c_edge = fmt(c_wr, c_base, c_n, c_avg)
            pwr_s, p_avg_s, p_edge = fmt(p_wr, p_base, p_n, p_avg)
            print(f"  +/-${t:>5.2f}M {c_row['n_events']:>4} | {cwr_s} {c_avg_s} {c_edge} | {pwr_s} {p_avg_s} {p_edge}")

    print(f"\n=== INTERPRETATION ===")
    print(f"EARN_WK = ticker with earnings within ±7 days (pre-event positioning)")
    print(f"EARN_MTH = earnings 8-30 days out (medium horizon)")
    print(f"NO_EARN = no earnings ahead within 30 days (organic flow only)")
    print(f"\nIf EARN_WK shows higher edge at $X+ thresholds → user's intuition correct:")
    print(f"earnings is the catalyst that justifies large flow")
    print(f"\nIf NO_EARN shows the cleaner edge at lower thresholds → 'someone always knows'")
    print(f"thesis works for organic flow, not pre-event positioning")
    print(f"\nSaved → v4_eod_surge_regime_sweep.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
