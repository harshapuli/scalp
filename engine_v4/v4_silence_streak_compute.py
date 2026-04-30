"""
V4 SILENCE-STREAK COMPUTE — detect "silent N days then BREAK" pattern.

User insight (AMD/NVDA observation): "AMD and NVDA were silent for long
time then exploded. We need to track that."

Backtest evidence: NVDA, META, AMZN, AMD all showed this pattern in our
6-day window — multiple silent days followed by a sudden surge.
The institutional positioning is BIGGER when it follows silence (pent-up
energy releasing) vs when it follows other recent surges (continuation).

This script computes per ticker:
  - silence_streak_days: consecutive sessions with |z_score| < 0.5
                         (looking back from yesterday)
  - today_z: current session's z_score (sign matters — call surge vs put)
  - is_breakout: silence_streak_days >= 3 AND |today_z| >= 1.5
  - breakout_side: 'CALL' or 'PUT' if is_breakout

Reads:
  data/eod_flow_history_60d.json (per-ticker per-day call_m)
  data/eod_flow_baselines.json   (per-ticker median + std)
  v4_snapshot_<latest>.json      (today's session)

Writes:
  data/silence_streak_state.json

Schedule: every 5 min during market hours (cheap — same data the
intraday surge compute already reads).
"""
import os, sys, json, glob, statistics
from datetime import datetime, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data', 'silence_streak_state.json')

SILENT_THRESHOLD = 1.0    # |z| < 1.0 = silent day (relaxed from 0.5 — pure quiet too rare)
BREAK_THRESHOLD = 1.5     # |z| >= 1.5 = breakout
MIN_SILENT_DAYS = 3       # need ≥3 silent days for the "after silence" pattern


def latest_snapshot_path():
    paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    return paths[-1] if paths else None


def compute_today_call_m(blob):
    """Sum last_30m net_call_premium from the snapshot's tick stream."""
    npt = blob.get('net_prem_ticks')
    if isinstance(npt, dict): npt = npt.get('data') or []
    if not isinstance(npt, list): return None
    s = 0.0; n = 0
    for t in npt:
        hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
        if '19:30' <= hh_mm <= '20:00':
            n += 1
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
    return (s / 1e6) if n else None


def main():
    hist_path = os.path.join(HERE, 'data', 'eod_flow_history_60d.json')
    base_path = os.path.join(HERE, 'data', 'eod_flow_baselines.json')
    if not (os.path.exists(hist_path) and os.path.exists(base_path)):
        print('missing history or baselines', file=sys.stderr); return 1
    history = json.load(open(hist_path))['history']
    baselines = json.load(open(base_path))['baselines']

    snap_path = latest_snapshot_path()
    snap = json.load(open(snap_path)).get('data') or {} if snap_path else {}

    today_iso = date.today().isoformat()
    out_per_ticker = {}
    n_breakouts = 0

    for tkr, days in history.items():
        b = baselines.get(tkr)
        if not b or b.get('std_floor_m', 0) <= 0: continue

        # Compute z per historical day
        sorted_days = sorted([d for d in days if days[d].get('call_m') is not None])
        if not sorted_days: continue
        z_history = []
        for d in sorted_days:
            cm = days[d]['call_m']
            z = (cm - b['median_m']) / b['std_floor_m']
            z_history.append((d, round(z, 3)))

        # Count consecutive silent days at the END of the history (most recent silent run)
        silent_streak = 0
        for d, z in reversed(z_history):
            if abs(z) < SILENT_THRESHOLD: silent_streak += 1
            else: break

        # Today's z (from snapshot data, since today not in 60d history yet)
        today_call_m = None
        today_z = None
        blob = snap.get(tkr)
        if blob:
            today_call_m = compute_today_call_m(blob)
            if today_call_m is not None:
                today_z = (today_call_m - b['median_m']) / b['std_floor_m']
                today_z = round(today_z, 3)

        # If today has data, check if it BREAKS the silence
        is_breakout = False
        breakout_side = None
        if today_z is not None and abs(today_z) >= BREAK_THRESHOLD and silent_streak >= MIN_SILENT_DAYS:
            is_breakout = True
            breakout_side = 'CALL' if today_z > 0 else 'PUT'
            n_breakouts += 1

        # Compute silence-streak strength: longer silence + bigger break = bigger conviction
        breakout_strength = 0.0
        if is_breakout and today_z is not None:
            breakout_strength = abs(today_z) * (1 + min(silent_streak, 10) / 5)
            breakout_strength = round(breakout_strength, 2)

        out_per_ticker[tkr] = {
            'ticker': tkr,
            'silent_streak_days': silent_streak,
            'today_call_m': round(today_call_m, 3) if today_call_m is not None else None,
            'today_z': today_z,
            'is_breakout': is_breakout,
            'breakout_side': breakout_side,
            'breakout_strength': breakout_strength,
            'baseline_quality': b.get('quality'),
            'baseline_n_days': b.get('n_days'),
            'recent_z_history': z_history[-10:],  # last 10 days for tooltip
        }

    out = {
        'generated_utc':       datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'today_iso':           today_iso,
        'thresholds':          {
            'silent_threshold': SILENT_THRESHOLD,
            'break_threshold':  BREAK_THRESHOLD,
            'min_silent_days':  MIN_SILENT_DAYS,
        },
        'n_tickers':           len(out_per_ticker),
        'n_breakouts':         n_breakouts,
        'tickers':             out_per_ticker,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f'Computed silence-streak for {len(out_per_ticker)} tickers')
    print(f'  Breakouts today (silent ≥{MIN_SILENT_DAYS}d + |z|≥{BREAK_THRESHOLD}σ today): {n_breakouts}')
    if n_breakouts:
        breakers = [r for r in out_per_ticker.values() if r['is_breakout']]
        breakers.sort(key=lambda r: -r['breakout_strength'])
        print(f'\n  Top breakouts (silence + magnitude):')
        for r in breakers[:15]:
            sign = '+' if r['breakout_side'] == 'CALL' else '-'
            t_call = r.get('today_call_m', 0) or 0
            print(f"    {r['ticker']:<6} silent {r['silent_streak_days']}d → today z={r['today_z']:+.2f}σ "
                  f"({r['breakout_side']}) ${t_call:+.1f}M  strength={r['breakout_strength']}")

    print(f'\nSaved → {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
