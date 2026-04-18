"""
QUALITY ANALYSIS — bucket every signal's premium + SMC magnitude, track win rates.

Hypothesis (testable after 30+ samples):
  Higher PREMIUM bucket + higher SMC bucket → higher win rate
  → Eventually replaces simple "score >= 40" gate with quality-weighted decision

NO ENGINE CHANGES YET. Pure analysis layer. Read-only on ledger + audit.

Run: python3 engine_v4/quality_analysis.py
Frequency: daily after close. Aggregate output saved to quality_buckets.json.
"""
import os
import json
from datetime import datetime
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(BASE_DIR, 'v4_ledger.json')
AUDIT_PATH = os.path.join(BASE_DIR, 'v4_rejection_audit.json')
OUT_PATH = os.path.join(BASE_DIR, 'quality_buckets.json')


def premium_bucket(score):
    if score is None: return 'unknown'
    s = float(score)
    if s < 1: return '0-1'
    if s < 2: return '1-2'
    if s < 3: return '2-3'
    return '3+'


def smc_bucket(score):
    if score is None: return 'unknown'
    s = float(score)
    if s == 0: return '0'
    if s <= 10: return '1-10'
    if s <= 15: return '11-15'
    if s < 20: return '16-19'
    return '20'


def quality_pass(prem, smc):
    """The hypothesis being tested: prem >= 3 AND smc == 20."""
    try:
        return float(prem) >= 3.0 and float(smc) >= 20
    except: return False


def load_outcomes():
    """Map (ticker, timestamp) → forward returns from analyzer audit."""
    if not os.path.exists(AUDIT_PATH): return {}
    with open(AUDIT_PATH) as f: audit = json.load(f)
    out = {}
    for r in audit.get('shadow_records', []) + audit.get('trigger_records', []):
        key = (r.get('Ticker'), r.get('Timestamp', ''))
        out[key] = r.get('Fwd_Ret', {})
    return out


def main():
    if not os.path.exists(LEDGER_PATH):
        print("No ledger found.")
        return
    with open(LEDGER_PATH) as f: ledger = json.load(f)
    outcomes = load_outcomes()

    # Pull all WATCH and TRIGGER entries (they have Score_Matrix)
    relevant = [e for e in ledger if 'WATCH' in e.get('Status','') or 'TRIGGER' in e.get('Status','')]

    print(f"\n{'='*70}")
    print(f"QUALITY BUCKET ANALYSIS — {len(relevant)} watch/trigger events")
    print(f"{'='*70}\n")

    # Bucket data
    by_premium = defaultdict(lambda: {'n': 0, 'wins': 0, 'losses': 0, 'flat': 0, 'sum': 0, 'open': 0})
    by_smc = defaultdict(lambda: {'n': 0, 'wins': 0, 'losses': 0, 'flat': 0, 'sum': 0, 'open': 0})
    by_quality = defaultdict(lambda: {'n': 0, 'wins': 0, 'losses': 0, 'flat': 0, 'sum': 0, 'open': 0})
    enriched_records = []

    for e in relevant:
        matrix = e.get('Score_Matrix') or {}
        prem = matrix.get('persistence', 0)
        smc = matrix.get('smc', 0)
        pb = premium_bucket(prem)
        sb = smc_bucket(smc)
        qp = quality_pass(prem, smc)

        key = (e.get('Ticker'), e.get('Timestamp', ''))
        fwd = outcomes.get(key, {})
        eod = fwd.get('EOD')
        if eod is None: eod = fwd.get('4H')

        for bucket_dict, bucket_key in [(by_premium, pb), (by_smc, sb), (by_quality, 'pass' if qp else 'fail')]:
            b = bucket_dict[bucket_key]
            b['n'] += 1
            if eod is None:
                b['open'] += 1
            else:
                b['sum'] += eod
                if eod > 0.005: b['wins'] += 1
                elif eod < -0.005: b['losses'] += 1
                else: b['flat'] += 1

        enriched_records.append({
            'ticker': e.get('Ticker'),
            'timestamp': e.get('Timestamp'),
            'status': e.get('Status'),
            'score': e.get('Confidence'),
            'premium_value': prem,
            'smc_value': smc,
            'premium_bucket': pb,
            'smc_bucket': sb,
            'quality_pass': qp,
            'eod_pct': round(eod * 100, 3) if eod is not None else None,
        })

    def fmt_bucket(label, bucket):
        n = bucket['n']
        if n == 0: return f'  {label:>10}: n=0'
        complete = n - bucket['open']
        if complete == 0: return f'  {label:>10}: n={n:>3} (all open)'
        win_rate = bucket['wins'] / complete * 100 if complete else 0
        avg = bucket['sum'] / complete * 100 if complete else 0
        bar = '█' * int(min(20, win_rate / 5))
        return f'  {label:>10}: n={n:>3} | {bucket["wins"]:>2}W / {bucket["losses"]:>2}L / {bucket["flat"]:>2}F | win {win_rate:>4.0f}% | avg {avg:+5.2f}% {bar}'

    print('=== By PREMIUM bucket ===')
    for label in ['3+', '2-3', '1-2', '0-1']:
        print(fmt_bucket(label, by_premium[label]))
    print()
    print('=== By SMC bucket ===')
    for label in ['20', '16-19', '11-15', '1-10', '0']:
        print(fmt_bucket(label, by_smc[label]))
    print()
    print('=== Quality gate (prem≥3 AND smc=20) ===')
    print(fmt_bucket('PASS', by_quality['pass']))
    print(fmt_bucket('FAIL', by_quality['fail']))

    # Save aggregated state — accumulates over runs for trend analysis
    summary = {
        'last_run_utc': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'total_events': len(relevant),
        'premium_buckets': {k: dict(v) for k, v in by_premium.items()},
        'smc_buckets': {k: dict(v) for k, v in by_smc.items()},
        'quality_gate': {k: dict(v) for k, v in by_quality.items()},
        'records': enriched_records,
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f'\n💾 Full data: {OUT_PATH}')
    print(f'   {len(enriched_records)} enriched records — open in Excel for slicing.')

    # Verdict tracker — how many days of data do we have?
    if len(enriched_records) < 30:
        print(f'\n⏳ STATISTICAL VERDICT: insufficient data ({len(enriched_records)} samples, need 30+)')
        print(f'   Run daily for 5-7 sessions before drawing conclusions.')
    else:
        print(f'\n📊 Sample size sufficient — patterns above can be acted on.')


if __name__ == "__main__":
    main()
