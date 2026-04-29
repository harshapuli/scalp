"""
V4 ACTION LABEL BACKTEST — does the composite "BUY/WAIT/PUT/SKIP" label work?

Each chip on the bucket page has been individually backtested. The COMPOSITE
action label (BucketView's actionFor) combines them with rules. This script
replays the composite on historical state and measures whether the combined
signal produces edge above its components.

Method:
  1. For each (ticker, day) where we have all relevant state — patrol
     archive, conviction snapshot, staging features from snapshot, baseline
     from eod_flow_baselines — compute what action label we WOULD have shown.
  2. Look up forward 1d / 3d returns.
  3. Report per-label hit rate, average return, vs baseline universe.

Limitations now:
  - patrol_history has only 1 archived day (logger started today)
  - conviction history has 4 archived days (Apr 26-29)
  - Snapshots cover 6 days, so we can derive flow + staging features
    even for days without patrol archives — but the patrol verdict
    will be None, which makes most action paths degrade to HOLD/FORMING.
  - Sample will be tiny initially. Re-run weekly as archives accumulate.

Run: python3 v4_action_label_backtest.py
Output: v4_action_label_backtest.json
"""
import os, sys, json, glob, statistics
from datetime import datetime, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5  # % aligned move counted as a hit


def date_from(p, prefix='v4_snapshot_'):
    return os.path.basename(p).replace(prefix, '').replace('.json', '')


def regime_for(earnings_str, session_iso):
    if not earnings_str: return None, None
    try:
        from datetime import datetime as _dt
        e = _dt.strptime(earnings_str, '%Y-%m-%d').date()
        s = _dt.strptime(session_iso, '%Y-%m-%d').date()
        days = abs((e - s).days)
        if days <= 7:  return 'EARN_WK',  days
        if days <= 30: return 'EARN_MTH', days
        return 'NO_EARN', days
    except: return None, None


def action_for(r):
    """Python port of BucketView.tsx actionFor() — must stay in sync."""
    v = r.get('patrol_verdict')
    cv = r.get('cv_verdict')
    u  = r.get('cv_urgency')
    inst = (r.get('positioning_score') or 0) >= 60
    day_pct = r.get('day_pct') or 0
    has_flip = r.get('has_flip', False)
    last_hr = r.get('last_hr_call_m')
    reg = r.get('earnings_regime')
    z = r.get('z_score')
    bq = r.get('baseline_quality')

    # 0. EOD SURGE (z-score primary, abs-$ fallback)
    if last_hr is not None and not has_flip:
        # Path A — z-score
        if z is not None and bq in ('STRONG', 'USABLE'):
            if z >= 2.0: return 'BUY'
            if z >= 1.5: return 'BUY'
            if -1.5 < z <= -0.5: return 'PUT'
            if z <= -1.5 and reg in ('EARN_WK', 'EARN_MTH'): return 'PUT'
        # Path B — absolute-$ fallback
        elif reg:
            if reg == 'EARN_WK' and -2.0 <= last_hr <= -0.5:  return 'PUT'
            if reg == 'EARN_MTH' and -2.0 <= last_hr <= -0.5: return 'PUT'
            if reg == 'EARN_MTH' and 0.5 <= last_hr <= 3.0:   return 'BUY'
            if reg == 'NO_EARN' and 1.0 <= last_hr <= 4.0:    return 'BUY'

    # 1. FLIP override
    if has_flip: return 'SKIP'

    # 2. Strong distribution
    if v == 'STRONG_DIST': return 'PUT'
    if v == 'DIST' and inst: return 'PUT'
    if v == 'DIST' and r.get('is_put_buy'): return 'PUT'
    if v == 'DIST': return 'SKIP'

    # 3. STRONG_ACC
    if v == 'STRONG_ACC':
        if u in ('EXHAUSTED', 'TOO_LATE') or day_pct >= 8: return 'WAIT'
        return 'BUY'

    # 4. ACC
    if v == 'ACC':
        if cv == 'BUY' and u in ('PULLBACK', 'EXTENSION'): return 'BUY'
        if cv == 'BUY' and u in ('HIT', 'ENTRY', 'MARKET') and day_pct < 5: return 'BUY'
        if cv == 'BUY' and u in ('EXHAUSTED', 'TOO_LATE'): return 'WAIT'
        if day_pct >= 7: return 'WAIT'
        if inst or cv == 'WATCH': return 'BUY'
        return 'FORMING'

    # 5. Neutral patrol with secondary signals
    if r.get('is_stealth'): return 'FORMING'
    if r.get('is_buy'): return 'FORMING'
    if cv == 'BUY' and u in ('PULLBACK', 'HIT'): return 'FORMING'
    if r.get('is_stealth_dist') or r.get('is_put_buy'): return 'PUT_WAIT'

    return 'HOLD'


def main():
    # ── Load picks ──
    picks = json.load(open(os.path.join(HERE, 'data', 'picks_77.json')))
    picks_by_t = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (picks.get('buckets') or {}).get(k, []) or []:
            picks_by_t[r['ticker']] = {**r, 'bucket': k}

    # ── Baselines (current snapshot) ──
    baselines = {}
    bp = os.path.join(HERE, 'data', 'eod_flow_baselines.json')
    if os.path.exists(bp):
        baselines = (json.load(open(bp)).get('baselines') or {})

    # ── Load all snapshots (per-ticker frozen state for each day) ──
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())

    # ── Load patrol_history archives ──
    patrol_history = {}
    for p in sorted(glob.glob(os.path.join(HERE, 'data', 'patrol_history', '*.json'))):
        d = date_from(p, prefix='')
        try:
            arc = json.load(open(p))
            patrol_history[d] = (arc.get('tickers') or {})
        except: pass

    # ── Load conviction archives ──
    conviction_history = {}
    for p in sorted(glob.glob(os.path.join(HERE, 'v4_conviction_2026-*.json'))):
        d = date_from(p, prefix='v4_conviction_')
        try:
            cv = json.load(open(p))
            conviction_history[d] = {r['ticker']: r for r in (cv.get('results') or [])}
        except: pass

    print(f'snapshots:  {dates}', file=sys.stderr)
    print(f'patrol arc: {sorted(patrol_history.keys())}', file=sys.stderr)
    print(f'cv arc:     {sorted(conviction_history.keys())}', file=sys.stderr)

    # ── Compute per (ticker, day) state and run action_for() ──
    events = []
    for i, d in enumerate(dates):
        # Need ≥1 day forward for return measurement
        if i + 1 >= len(dates): continue

        for tkr, blob in snaps[d].items():
            if tkr not in picks_by_t: continue

            # Earnings regime (computed per session)
            earn_str = picks_by_t[tkr].get('next_earnings_date')
            reg, days_to_earn = regime_for(earn_str, d)

            # Last_hr_call_m from snapshot's net_prem_ticks (use 19:00-20:00 UTC)
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            last_hr_call_m = None
            if isinstance(npt, list) and npt:
                wcall = 0.0
                ticks = 0
                for t in npt:
                    hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                    if '19:00' <= hh_mm <= '20:00':
                        ticks += 1
                        try: wcall += float(t.get('net_call_premium', 0) or 0)
                        except: pass
                if ticks: last_hr_call_m = wcall / 1e6

            # z-score from baseline
            b = baselines.get(tkr)
            z_score, baseline_quality = None, None
            if b and last_hr_call_m is not None and b.get('std_floor_m', 0) > 0:
                z_score = (last_hr_call_m - b['median_m']) / b['std_floor_m']
                baseline_quality = b.get('quality')

            # Patrol state (only for days with archive)
            patrol_state = (patrol_history.get(d) or {}).get(tkr) or {}
            alerts = patrol_state.get('alerts') or []
            acc_n = sum(1 for a in alerts if 'ACC' in (a.get('type') or ''))
            dist_n = sum(1 for a in alerts if 'DIST' in (a.get('type') or ''))
            has_flip = False
            prev = None
            for a in alerts:
                side = 'ACC' if 'ACC' in (a.get('type') or '') else ('DIST' if 'DIST' in (a.get('type') or '') else None)
                if side and prev and prev != side: has_flip = True
                if side: prev = side
            patrol_factors = patrol_state.get('factors') or {}
            positioning_score = patrol_state.get('positioning_score')

            # Conviction state
            cvr = (conviction_history.get(d) or {}).get(tkr) or {}
            cv_verdict = cvr.get('verdict')
            cv_urgency = (cvr.get('trade_idea') or {}).get('entry_urgency') or cvr.get('urgency')
            day_pct_cv = cvr.get('day_pct')
            is_stealth = cvr.get('is_stealth') or False
            is_stealth_dist = cvr.get('is_stealth_dist') or False

            # Staging signals — compute is_buy/is_put_buy from snapshot features
            # Skip for now; would need to run the staging scanner against this day.
            # TODO: run staging_score_v8 on each historical day's blob.

            # Build state record
            state = {
                'ticker': tkr, 'date': d, 'bucket': picks_by_t[tkr]['bucket'],
                'patrol_verdict':  patrol_state.get('verdict'),
                'patrol_score':    patrol_state.get('score'),
                'positioning_score': positioning_score,
                'has_flip': has_flip,
                'acc_n': acc_n, 'dist_n': dist_n,
                'cv_verdict': cv_verdict,
                'cv_urgency': cv_urgency,
                'day_pct': day_pct_cv,
                'is_stealth': is_stealth,
                'is_stealth_dist': is_stealth_dist,
                'last_hr_call_m': last_hr_call_m,
                'earnings_regime': reg,
                'days_to_earnings': days_to_earn,
                'z_score': z_score,
                'baseline_quality': baseline_quality,
                # Staging not available historically yet
                'is_buy': False,
                'is_put_buy': False,
            }
            action = action_for(state)
            state['action'] = action

            # Forward returns from snapshots
            today_close = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            def fwd_close(idx):
                if idx >= len(dates): return None
                fblob = snaps[dates[idx]].get(tkr)
                if not fblob: return None
                return ((fblob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            f1 = fwd_close(i + 1)
            f3 = fwd_close(i + 3)
            ret_1d = ret_3d = None
            try:
                if today_close and f1: ret_1d = (float(f1) - float(today_close)) / float(today_close) * 100
                if today_close and f3: ret_3d = (float(f3) - float(today_close)) / float(today_close) * 100
            except: pass
            state['ret_1d'] = round(ret_1d, 3) if ret_1d is not None else None
            state['ret_3d'] = round(ret_3d, 3) if ret_3d is not None else None
            events.append(state)

    print(f'\ntotal (ticker, day) records: {len(events)}', file=sys.stderr)

    # ── Aggregate by action label ──
    def stats(group, side='LONG'):
        sign = 1 if side == 'LONG' else -1 if side == 'SHORT' else 0
        for hz, key in [('1d', 'ret_1d'), ('3d', 'ret_3d')]:
            pass
        out = {'n_total': len(group)}
        for hz, key in (('1d', 'ret_1d'), ('3d', 'ret_3d')):
            vals = [r[key] for r in group if r.get(key) is not None]
            if not vals:
                out[f'{hz}_n'] = 0
                continue
            if side == 'NEUTRAL':
                wins_up = sum(1 for v in vals if v >= WIN_THRESHOLD)
                wins_dn = sum(1 for v in vals if v <= -WIN_THRESHOLD)
                out[f'{hz}_n'] = len(vals)
                out[f'{hz}_up_pct'] = round(wins_up / len(vals) * 100, 1)
                out[f'{hz}_down_pct'] = round(wins_dn / len(vals) * 100, 1)
                out[f'{hz}_avg'] = round(statistics.mean(vals), 2)
            else:
                wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
                out[f'{hz}_n'] = len(vals)
                out[f'{hz}_aligned_win%'] = round(wins / len(vals) * 100, 1)
                out[f'{hz}_aligned_avg'] = round(statistics.mean(v * sign for v in vals), 2)
        return out

    side_for = {
        'BUY': 'LONG', 'WAIT': 'LONG', 'FORMING': 'LONG',
        'PUT': 'SHORT', 'PUT_WAIT': 'SHORT',
        'SKIP': 'NEUTRAL', 'HOLD': 'NEUTRAL',
    }
    by_action = defaultdict(list)
    for e in events: by_action[e['action']].append(e)
    summary = {label: stats(group, side_for.get(label, 'NEUTRAL'))
               for label, group in by_action.items()}

    # Per-bucket × action breakdown
    by_bucket_action = defaultdict(lambda: defaultdict(list))
    for e in events:
        by_bucket_action[e['bucket']][e['action']].append(e)

    bucket_action_summary = {}
    for bucket in ('mega', 'mid', 'small', 'indices'):
        d = by_bucket_action.get(bucket) or {}
        bucket_action_summary[bucket] = {
            'n_total': sum(len(g) for g in d.values()),
            'baseline_long':  stats([e for g in d.values() for e in g], 'LONG'),
            'baseline_short': stats([e for g in d.values() for e in g], 'SHORT'),
            'by_action': {
                label: stats(group, side_for.get(label, 'NEUTRAL'))
                for label, group in d.items()
            },
        }

    # Universe baselines (no filter)
    universe_long  = stats(events, 'LONG')
    universe_short = stats(events, 'SHORT')

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'win_threshold_pct': WIN_THRESHOLD,
        'sessions': dates,
        'patrol_archives': sorted(patrol_history.keys()),
        'conviction_archives': sorted(conviction_history.keys()),
        'total_events': len(events),
        'universe_baseline_long':  universe_long,
        'universe_baseline_short': universe_short,
        'by_action_label': summary,
        'by_bucket_action': bucket_action_summary,
    }
    with open(os.path.join(HERE, 'v4_action_label_backtest.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n{'='*108}")
    print(f"V4 ACTION LABEL BACKTEST — does the composite chip work?")
    print(f"{len(events)} ticker-days from {dates[0]} → {dates[-1]}")
    print(f"Patrol archives: {len(patrol_history)} day(s)  ·  Conviction archives: {len(conviction_history)} day(s)")
    print(f"{'='*108}\n")
    print(f"UNIVERSE BASELINE (all events, no filter):")
    for hz in ('1d', '3d'):
        l = universe_long.get(f'{hz}_aligned_win%')
        s = universe_short.get(f'{hz}_aligned_win%')
        l_avg = universe_long.get(f'{hz}_aligned_avg')
        n = universe_long.get(f'{hz}_n', 0)
        print(f"  {hz}: long_win={l}%  short_win={s}%  avg={l_avg:+.2f}%  n={n}")

    print(f"\nPER-LABEL RESULTS:")
    print(f"{'action':<12} {'n':>5} | {'1d_n':>5} {'1d_win%':>9} {'1d_avg':>8} {'1d_edge':>8} | "
          f"{'3d_n':>5} {'3d_win%':>9} {'3d_avg':>8} {'3d_edge':>8}")
    print('-' * 110)
    label_order = ['BUY', 'WAIT', 'FORMING', 'HOLD', 'SKIP', 'PUT', 'PUT_WAIT']
    for label in label_order:
        if label not in summary: continue
        s = summary[label]
        side = side_for.get(label, 'NEUTRAL')
        if side == 'NEUTRAL':
            # Show both directions
            for hz in ('1d', '3d'):
                pass
            row = f"{label:<12} {s['n_total']:>5} |"
            for hz in ('1d', '3d'):
                u = s.get(f'{hz}_up_pct', '—')
                d = s.get(f'{hz}_down_pct', '—')
                a = s.get(f'{hz}_avg')
                a_s = f"{a:+.2f}%" if a is not None else '—'
                row += f" up={u}% down={d}% avg={a_s} |"
            print(row)
            continue
        base_key = 'aligned_win%'
        baseline = (universe_long if side == 'LONG' else universe_short)
        out_row = f"{label:<12} {s['n_total']:>5} |"
        for hz in ('1d', '3d'):
            n = s.get(f'{hz}_n', 0)
            wr = s.get(f'{hz}_aligned_win%')
            avg = s.get(f'{hz}_aligned_avg')
            base = baseline.get(f'{hz}_aligned_win%')
            edge = (wr - base) if (wr is not None and base is not None) else None
            wr_s = f"{wr}%" if wr is not None else '—'
            avg_s = f"{avg:+.2f}%" if avg is not None else '—'
            edge_s = f"{edge:+.1f}pp" if edge is not None else '—'
            out_row += f" {n:>5} {wr_s:>9} {avg_s:>8} {edge_s:>8} |"
        print(out_row)

    # Per-bucket × action breakdown
    print(f"\n{'='*108}")
    print(f"PER-BUCKET BREAKDOWN")
    print(f"{'='*108}")
    label_order = ['BUY', 'WAIT', 'FORMING', 'HOLD', 'SKIP', 'PUT', 'PUT_WAIT']
    for bucket in ('mega', 'mid', 'small', 'indices'):
        b = bucket_action_summary[bucket]
        bl, bs = b['baseline_long'], b['baseline_short']
        bl_3d = bl.get('3d_aligned_win%')
        bs_3d = bs.get('3d_aligned_win%')
        print(f"\n--- {bucket.upper()} ({b['n_total']} ticker-days)  ·  baseline 3d long={bl_3d}% short={bs_3d}% ---")
        print(f"{'action':<12} {'n':>5} | {'1d_n':>5} {'1d_win%':>9} {'1d_edge':>8} | {'3d_n':>5} {'3d_win%':>9} {'3d_edge':>8}")
        print('-' * 90)
        for label in label_order:
            if label not in b['by_action']: continue
            s = b['by_action'][label]
            side = side_for.get(label, 'NEUTRAL')
            if side == 'NEUTRAL':
                row = f"{label:<12} {s['n_total']:>5} |"
                for hz in ('1d', '3d'):
                    u = s.get(f'{hz}_up_pct', '—')
                    d = s.get(f'{hz}_down_pct', '—')
                    a = s.get(f'{hz}_avg')
                    a_s = f"{a:+.2f}%" if a is not None else '—'
                    row += f" up={u}% down={d}% avg={a_s} |"
                print(row)
                continue
            base = bl if side == 'LONG' else bs
            out_row = f"{label:<12} {s['n_total']:>5} |"
            for hz in ('1d', '3d'):
                n = s.get(f'{hz}_n', 0)
                wr = s.get(f'{hz}_aligned_win%')
                base_wr = base.get(f'{hz}_aligned_win%')
                edge = (wr - base_wr) if (wr is not None and base_wr is not None) else None
                wr_s = f"{wr}%" if wr is not None else '—'
                edge_s = f"{edge:+.1f}pp" if edge is not None else '—'
                out_row += f" {n:>5} {wr_s:>9} {edge_s:>8} |"
            print(out_row)

    print(f"\nNOTE: With patrol archives only on {sorted(patrol_history.keys())},")
    print(f"most ticker-days have patrol_verdict=None and degrade to FORMING/HOLD.")
    print(f"As patrol_archive_loop runs daily, this backtest gets stronger.")
    print(f"\nSaved → v4_action_label_backtest.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
