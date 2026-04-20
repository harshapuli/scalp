"""
V4 PATTERN ANALYZER — reads the append-only accumulation outcomes log and
produces win-rate breakdowns by theme, tier, score band, and pattern feature.

Goal: over weeks of data, surface WHICH kinds of scouts actually break out
so we can eventually weight them differently (bigger size on the ones that
work, smaller/no size on the ones that don't).

Input:
  v4_accumulation_outcomes_history.jsonl — one JSON line per (scout, eval_time)

Output:
  Console report: win rate by theme, tier, score band, range width, position
  v4_pattern_report.json — machine-readable aggregate

Usage:
  python3 engine_v4/v4_pattern_analyzer.py              # all data
  python3 engine_v4/v4_pattern_analyzer.py --since=7    # last 7 days only

Run weekly. After 2-3 weeks, themes with consistently high win rates can be
acted on (raise size multiplier), and consistently weak themes can be dropped.
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(BASE_DIR, 'v4_accumulation_outcomes_history.jsonl')
REPORT_PATH = os.path.join(BASE_DIR, 'v4_pattern_report.json')


def parse_args():
    since_days = None
    for arg in sys.argv[1:]:
        if arg.startswith('--since='):
            try: since_days = int(arg.split('=', 1)[1])
            except ValueError: pass
    return since_days


def load_history(since_days=None):
    if not os.path.exists(HISTORY_PATH):
        print(f"⚠️  No history yet. Run v4_accumulation_tracker.py first (or let armada do it).")
        return []
    records = []
    cutoff = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    with open(HISTORY_PATH) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                r = json.loads(line)
                if cutoff:
                    emit_str = r.get('emit_date', '')
                    if emit_str:
                        emit_dt = datetime.strptime(emit_str, '%Y-%m-%d').replace(tzinfo=UTC)
                        if emit_dt < cutoff: continue
                records.append(r)
            except Exception: continue

    # Dedup to the MOST RECENT evaluation per (ticker, emit_date)
    latest = {}
    for r in records:
        key = (r.get('ticker'), r.get('emit_date'))
        prev = latest.get(key)
        if not prev or r.get('logged_utc', '') > prev.get('logged_utc', ''):
            latest[key] = r
    return list(latest.values())


def classify_outcome(r):
    """Normalize outcome into a simple bucket: WIN / LOSS / OPEN."""
    outcome = r.get('outcome')
    if outcome == 'HIT_TARGET': return 'WIN'
    if outcome == 'HIT_STOP':   return 'LOSS'
    # Use same-day peak as a proxy if still OPEN: if peaked within 1% of target, count as soft-win
    if outcome == 'OPEN':
        peak = r.get('peak_pct')
        if peak is None: return 'OPEN'
        target_pct = r.get('target_pct_away')
        if target_pct and peak >= target_pct * 0.9:
            return 'WIN'  # came close enough
    return 'OPEN'


def pct(win, total):
    return (win / total * 100) if total else 0


def group_stats(rows, group_fn, label):
    """Bucket rows by group_fn and compute win/loss/open counts + avg peak move."""
    buckets = defaultdict(lambda: {'win': 0, 'loss': 0, 'open': 0, 'peak_sum': 0, 'n_peak': 0})
    for r in rows:
        key = group_fn(r)
        if key is None: continue
        b = buckets[key]
        cls = classify_outcome(r)
        if cls == 'WIN':  b['win'] += 1
        elif cls == 'LOSS': b['loss'] += 1
        else: b['open'] += 1
        peak = r.get('peak_pct')
        if peak is not None:
            b['peak_sum'] += peak; b['n_peak'] += 1

    print(f"\n=== {label} ===")
    print(f"  {'group':<30} {'W':>3} {'L':>3} {'O':>3} {'N':>4} {'win_rate':>9} {'avg_peak':>9}")
    rows_out = []
    for key, b in sorted(buckets.items(), key=lambda kv: -(kv[1]['win'] / max(1, kv[1]['win']+kv[1]['loss']+kv[1]['open']))):
        total = b['win'] + b['loss'] + b['open']
        resolved = b['win'] + b['loss']
        wr = pct(b['win'], resolved) if resolved >= 3 else None  # need 3+ resolved to report
        avg_peak = (b['peak_sum'] / b['n_peak']) if b['n_peak'] else None
        wr_str = f"{wr:.0f}%" if wr is not None else '—'
        pk_str = f"{avg_peak:+.2f}%" if avg_peak is not None else '—'
        print(f"  {str(key):<30} {b['win']:>3} {b['loss']:>3} {b['open']:>3} {total:>4}  {wr_str:>8} {pk_str:>9}")
        rows_out.append({'group': key, **b, 'win_rate': wr, 'avg_peak': avg_peak})
    return rows_out


def main():
    since_days = parse_args()
    rows = load_history(since_days)
    if not rows:
        print("No data to analyze yet.")
        return

    print(f"\n{'='*90}")
    print(f"V4 PATTERN ANALYZER — {len(rows)} unique scout outcomes" +
          (f" (last {since_days}d)" if since_days else ' (all history)'))
    print(f"{'='*90}")

    # Overall stats
    total = len(rows)
    wins = sum(1 for r in rows if classify_outcome(r) == 'WIN')
    losses = sum(1 for r in rows if classify_outcome(r) == 'LOSS')
    open_n = sum(1 for r in rows if classify_outcome(r) == 'OPEN')
    resolved = wins + losses
    print(f"\nOverall: {wins}W / {losses}L / {open_n} open  (win rate: {pct(wins, resolved):.0f}% of resolved)")

    # Bucket by theme
    by_theme = group_stats(rows, lambda r: r.get('theme') or 'Other', 'BY THEME')

    # Bucket by tier
    by_tier = group_stats(rows, lambda r: f"Tier {r.get('tier')}", 'BY TIER')

    # Bucket by score band
    def score_band(r):
        s = r.get('score')
        if s is None: return None
        if s < 45: return '40-44'
        if s < 50: return '45-49'
        if s < 55: return '50-54'
        if s < 60: return '55-59'
        return '60+'
    by_score = group_stats(rows, score_band, 'BY SCORE BAND')

    # Bucket by position-in-range (where in the compression zone was price when scout fired)
    def pos_band(r):
        p = r.get('pos_in_range_pct')
        if p is None: return None
        if p < 40: return 'bottom (<40%)'
        if p < 70: return 'middle (40-70%)'
        if p < 90: return 'upper (70-90%)'
        return 'top (>=90%)'
    by_pos = group_stats(rows, pos_band, 'BY POSITION IN RANGE')

    # Bucket by range width
    def width_band(r):
        w = r.get('range_width_pct')
        if w is None: return None
        if w < 10: return 'tight (<10%)'
        if w < 20: return 'moderate (10-20%)'
        if w < 35: return 'wide (20-35%)'
        return 'very wide (>=35%)'
    by_width = group_stats(rows, width_band, 'BY RANGE WIDTH')

    # Bucket by distance to target
    def dist_band(r):
        d = r.get('dist_to_target_pct')
        if d is None: return None
        if d < 1: return '<1%'
        if d < 2: return '1-2%'
        if d < 3: return '2-3%'
        if d < 5: return '3-5%'
        return '>=5%'
    by_dist = group_stats(rows, dist_band, 'BY DISTANCE TO TARGET')

    # Save report
    payload = {
        'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'since_days': since_days,
        'total_records': total,
        'wins': wins, 'losses': losses, 'open': open_n,
        'resolved_win_rate': pct(wins, resolved),
        'by_theme': by_theme,
        'by_tier': by_tier,
        'by_score': by_score,
        'by_pos_in_range': by_pos,
        'by_range_width': by_width,
        'by_target_distance': by_dist,
    }
    with open(REPORT_PATH, 'w') as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n💾 Report saved: {REPORT_PATH}")
    print(f"\nInsight requires 20+ resolved outcomes per bucket. Run again after more data accumulates.")


if __name__ == "__main__":
    main()
