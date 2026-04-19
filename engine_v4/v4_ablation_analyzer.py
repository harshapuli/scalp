"""
V4 ABLATION ANALYZER — answers "which feature actually moves the needle?"

Reads the audit + ledger truth layer and computes per-feature contribution
to outcomes:

  For each scoring feature in Score_Matrix, partition signals by
  "feature contributed > 0 pts" vs "feature contributed = 0 pts" and
  report the difference in win rate, average return, and expectancy.

  Then run "leave-one-out" simulation: for each feature, what would the
  win rate look like if we'd ignored that feature's contribution to the
  total score (and therefore the gating decision)?

The goal is to answer the audit's question:
  "Is edge coming from flow? from compression? from squeeze? from market beta?"

INPUTS:
  v4_rejection_audit.json — analyzer output with Score_Matrix + Fwd_Ret per signal
  v4_paper_positions.json — actual paper trades with realized P&L

OUTPUTS:
  v4_ablation_report.json — per-feature ablation table
  Console — human-readable ranking

USAGE:
  python3 engine_v4/v4_ablation_analyzer.py

Re-run after every batch of new signals/positions to see the picture sharpen.
"""
import os
import sys
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_PATH = os.path.join(BASE_DIR, 'v4_rejection_audit.json')
LEDGER_PATH = os.path.join(BASE_DIR, 'v4_ledger.json')
POSITIONS_PATH = os.path.join(BASE_DIR, 'v4_paper_positions.json')
REPORT_PATH = os.path.join(BASE_DIR, 'v4_ablation_report.json')

WIN_THRESHOLD_OPT = 0.05   # ±5% on option premium
WIN_THRESHOLD_STK = 0.005  # ±0.5% on stock proxy

# All scorable features in Score_Matrix. Ordered by recency / criticality.
ALL_FEATURES = [
    # Original 8 features (live since the engine was built)
    'persistence', 'smc', 'dp', 'greek', 'iv', 'open_conf', 'tod', 'ask_dom',
    # Phase 1-5 truth layer (added 2026-04-18 commit af86749)
    'dp_real_pts', 'squeeze_pts', 'options_volume', 'catalyst',
    'iv_term_inverted', 'earnings_info',
]

# Features that are *contribute-to-score* (numeric) vs informational (bool/dict)
NUMERIC_FEATURES = {
    'persistence', 'smc', 'dp', 'greek', 'iv', 'open_conf', 'tod', 'ask_dom',
    'dp_real_pts', 'squeeze_pts',
}
BOOL_FEATURES = {'iv_term_inverted', 'catalyst'}
DICT_FEATURES = {'options_volume', 'earnings_info'}


def fmt_pct(x): return f"{x*100:+.2f}%" if x is not None else "—"


def load_signals_with_outcomes():
    """Build unified list of (signal, outcome) tuples.

    Source 1: audit triggers (with Score_Matrix from new analyzer)
    Source 2: paper positions (with realized P&L if closed)
    Falls back to ledger Score_Matrix if audit doesn't have it (older runs)."""
    with open(AUDIT_PATH) as f: audit = json.load(f)
    with open(LEDGER_PATH) as f: ledger = json.load(f)
    positions_by_ticker_date = {}
    if os.path.exists(POSITIONS_PATH):
        with open(POSITIONS_PATH) as f: positions = json.load(f)
        for p in positions:
            key = (p.get('ticker'), (p.get('entry_time_utc', '') or '')[:10])
            positions_by_ticker_date[key] = p

    # Build ledger lookup for Score_Matrix backfill
    ledger_lookup = {}
    for e in ledger:
        key = (e.get('Ticker'), e.get('Timestamp'))
        ledger_lookup[key] = e

    rows = []
    for r in audit.get('trigger_records', []):
        ticker = r.get('Ticker')
        ts = r.get('Timestamp', '')
        date = ts[:10] if ts else ''

        # Score_Matrix: prefer audit, fall back to ledger
        sm = r.get('Score_Matrix') or {}
        if not sm:
            led = ledger_lookup.get((ticker, ts))
            if led: sm = led.get('Score_Matrix') or {}

        # Outcome resolution priority:
        # 1. realized P&L from paper position (real money)
        # 2. option-premium 5D forward return (Return_Basis = option_premium)
        # 3. stock 5D forward return (proxy)
        outcome = None
        outcome_basis = None
        win_threshold = WIN_THRESHOLD_STK

        pos = positions_by_ticker_date.get((ticker, date))
        if pos and pos.get('status') == 'CLOSED' and pos.get('realized_pnl_pct') is not None:
            try:
                outcome = float(pos['realized_pnl_pct']) / 100.0
                outcome_basis = 'realized_pnl'
                win_threshold = WIN_THRESHOLD_OPT
            except: pass

        if outcome is None:
            ret_basis = r.get('Return_Basis', 'underlying_stock')
            fwd = r.get('Fwd_Ret') or {}
            for h in ('5D', 'EOD', '4H'):
                v = fwd.get(h)
                if v is not None:
                    outcome = v
                    outcome_basis = ret_basis + '_' + h
                    win_threshold = WIN_THRESHOLD_OPT if ret_basis == 'option_premium' else WIN_THRESHOLD_STK
                    break

        if outcome is None or not sm:
            continue

        rows.append({
            'ticker': ticker, 'date': date, 'direction': r.get('Type'),
            'status': r.get('Status'),
            'score_matrix': sm,
            'outcome': outcome,
            'outcome_basis': outcome_basis,
            'win_threshold': win_threshold,
        })
    return rows


def feature_value(sm: dict, feature: str):
    """Normalize feature to a comparable value: numeric pts, bool, or 'present'."""
    v = sm.get(feature)
    if v is None: return None
    if feature in BOOL_FEATURES: return bool(v)
    if feature in DICT_FEATURES: return bool(v)  # treat dict-valued as present/absent
    try: return float(v)
    except: return None


def feature_active(sm: dict, feature: str) -> bool:
    """True if feature contributed something nonzero / present."""
    v = feature_value(sm, feature)
    if v is None: return False
    if isinstance(v, bool): return v
    return abs(v) > 0.001  # non-zero contribution


def per_feature_stats(rows):
    """For each feature, partition rows by active vs inactive and report stats."""
    out = []
    for f in ALL_FEATURES:
        active = []
        inactive = []
        for r in rows:
            if feature_active(r['score_matrix'], f):
                active.append(r)
            else:
                inactive.append(r)
        if not active and not inactive:
            continue

        def group_stats(group):
            if not group:
                return {'n': 0, 'win_rate': None, 'avg_return': None,
                        'avg_win': None, 'avg_loss': None, 'expectancy': None}
            n = len(group)
            wins, losses, flat = 0, 0, 0
            for r in group:
                if r['outcome'] > r['win_threshold']: wins += 1
                elif r['outcome'] < -r['win_threshold']: losses += 1
                else: flat += 1
            avg = sum(r['outcome'] for r in group) / n
            win_returns = [r['outcome'] for r in group if r['outcome'] > 0]
            loss_returns = [r['outcome'] for r in group if r['outcome'] < 0]
            avg_win = (sum(win_returns) / len(win_returns)) if win_returns else None
            avg_loss = (sum(loss_returns) / len(loss_returns)) if loss_returns else None
            wr = wins / n if n else 0
            expectancy = (wr * (avg_win or 0)) + ((1 - wr) * (avg_loss or 0))
            return {
                'n': n, 'wins': wins, 'losses': losses, 'flat': flat,
                'win_rate': round(wr, 3),
                'avg_return': round(avg, 4),
                'avg_win': round(avg_win, 4) if avg_win is not None else None,
                'avg_loss': round(avg_loss, 4) if avg_loss is not None else None,
                'expectancy': round(expectancy, 4),
            }

        a = group_stats(active)
        i = group_stats(inactive)

        # Lift = (active win rate) − (inactive win rate). Positive = feature helps.
        lift = None
        if a['win_rate'] is not None and i['win_rate'] is not None:
            lift = round(a['win_rate'] - i['win_rate'], 3)
        # Expectancy delta — what does turning this feature ON add to expected return per trade?
        exp_delta = None
        if a['expectancy'] is not None and i['expectancy'] is not None:
            exp_delta = round(a['expectancy'] - i['expectancy'], 4)

        out.append({
            'feature': f,
            'active': a, 'inactive': i,
            'win_rate_lift': lift,
            'expectancy_delta': exp_delta,
        })
    return out


def main():
    print("\n" + "="*82)
    print("V4 ABLATION ANALYZER — per-feature contribution to outcomes")
    print("="*82)

    rows = load_signals_with_outcomes()
    print(f"\nUsable signals (with Score_Matrix + outcome): {len(rows)}")
    if not rows:
        print("\n⚠️  No signals with both Score_Matrix and outcome data yet.")
        print("    Run v4_ai_analyzer.py first to populate the audit.")
        print("    Then this analyzer will work as new signals accumulate.")
        return

    # Outcome basis distribution
    by_basis = defaultdict(int)
    for r in rows:
        by_basis[r['outcome_basis']] += 1
    print(f"Outcome basis distribution:")
    for b, c in sorted(by_basis.items(), key=lambda x: -x[1]):
        print(f"  {b}: {c}")
    print()

    stats = per_feature_stats(rows)

    # Report — sorted by win-rate lift (most discriminating features first)
    print(f"\n{'feature':<22} {'active_n':>9} {'a_win%':>7} {'a_exp':>9} {'inact_n':>9} {'i_win%':>7} {'i_exp':>9} {'WR_lift':>8} {'exp_Δ':>8}")
    print('-' * 100)
    by_lift = sorted(stats, key=lambda x: (x['win_rate_lift'] is None, -(x['win_rate_lift'] or 0)))
    for s in by_lift:
        a, i = s['active'], s['inactive']
        wr_lift = f"{s['win_rate_lift']*100:+.1f}pp" if s['win_rate_lift'] is not None else "—"
        exp_d = f"{s['expectancy_delta']*100:+.2f}%" if s['expectancy_delta'] is not None else "—"
        a_wr = f"{a['win_rate']*100:.1f}%" if a['win_rate'] is not None else "—"
        i_wr = f"{i['win_rate']*100:.1f}%" if i['win_rate'] is not None else "—"
        a_exp = f"{a['expectancy']*100:+.2f}%" if a['expectancy'] is not None else "—"
        i_exp = f"{i['expectancy']*100:+.2f}%" if i['expectancy'] is not None else "—"
        print(f"{s['feature']:<22} {a['n']:>9} {a_wr:>7} {a_exp:>9} {i['n']:>9} {i_wr:>7} {i_exp:>9} {wr_lift:>8} {exp_d:>8}")

    # Verdict per feature
    print("\n" + "="*82)
    print("VERDICTS — what to do with each feature")
    print("="*82)
    for s in sorted(stats, key=lambda x: -(x['win_rate_lift'] or -1)):
        a, i = s['active'], s['inactive']
        if a['n'] < 3 or i['n'] < 3:
            verdict = f"⏳ INSUFFICIENT DATA (need 5+ in each group, have {a['n']}/{i['n']})"
        elif s['win_rate_lift'] is None:
            verdict = "— no comparable data"
        elif s['win_rate_lift'] >= 0.10:
            verdict = "🟢 STRONG POSITIVE — keep, possibly weight higher"
        elif s['win_rate_lift'] >= 0.03:
            verdict = "🟢 modest positive — keep"
        elif s['win_rate_lift'] >= -0.03:
            verdict = "⚪ NEUTRAL — keep (signals nothing) or drop (no harm)"
        elif s['win_rate_lift'] >= -0.10:
            verdict = "🟠 modest negative — consider lowering weight"
        else:
            verdict = "🔴 STRONG NEGATIVE — DROP from scoring"
        print(f"  {s['feature']:<22} {verdict}")

    # Save report
    report = {
        'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'n_signals': len(rows),
        'outcome_basis_distribution': dict(by_basis),
        'per_feature_stats': stats,
    }
    with open(REPORT_PATH, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n💾 Saved: {REPORT_PATH}")
    print(f"\nNote: this analyzer becomes more reliable as more signals accumulate.")
    print(f"      Re-run after every 10-20 new triggers/positions for sharper conclusions.")


if __name__ == "__main__":
    main()
