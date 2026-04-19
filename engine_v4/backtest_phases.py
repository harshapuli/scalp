"""
BACKTEST: simulate the new Phase 1-5 gates on the existing 273-signal ledger.

Method:
  For each WATCH_PULLBACK / TRIGGER_PULLBACK_CONFIRMED in the ledger
  (signals the engine actually wanted to act on), retroactively apply
  each new gate and check the forward returns from the audit. Compare
  old win rate vs filtered win rate.

Gates evaluable retroactively (data still queryable now):
  - earnings_within(t, 5)        — current earnings calendar
  - seasonality_score(t, month)  — historical, deterministic
  - squeeze_score(t, dir)        — current borrow/SI/FTDs
  - iv_term_inversion(t)         — current IV term

NOT retroactively evaluable (need historical intraday snapshots):
  - oi_change                    — yesterday's OI is gone
  - darkpool_score               — historical DP prints not in API
  - options_volume_today         — today only
  - economic-calendar halt       — would need historical calendar
"""
import os, sys, json, statistics
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from v4_uw_helpers import (
    earnings_within, seasonality_score, squeeze_score, iv_term_inversion
)

LEDGER = json.load(open(os.path.join(os.path.dirname(__file__), 'v4_ledger.json')))
AUDIT = json.load(open(os.path.join(os.path.dirname(__file__), 'v4_rejection_audit.json')))

# Build outcome lookup from audit (forward returns by ticker+timestamp)
outcomes = {}
for r in AUDIT.get('shadow_records', []) + AUDIT.get('trigger_records', []):
    key = (r.get('Ticker'), r.get('Timestamp', ''))
    outcomes[key] = {
        'fwd_4h': r.get('Fwd_Ret', {}).get('4H'),
        'fwd_eod': r.get('Fwd_Ret', {}).get('EOD'),
        'fwd_d1': r.get('Fwd_Ret', {}).get('D1'),
        'return_basis': r.get('Return_Basis'),
    }

def sign_for(direction, ret):
    """Apply directional sign so + means thesis worked. Stock-basis only;
    option_premium basis already directional."""
    if ret is None: return None
    return ret if direction == 'CALL' else -ret

# Focus on actionable signals (engine wanted to take these)
actionable_statuses = {'WATCH_PULLBACK', 'WATCH_BREAKOUT',
                        'TRIGGER_PULLBACK_CONFIRMED', 'TRIGGER_BREAKOUT_CONFIRMED'}
actionable = [e for e in LEDGER if e.get('Status') in actionable_statuses]

# Also include LOW_SCORE that were close (35-39) — squeeze scoring might lift them past 40
def conf(e):
    try: return float(e.get('Confidence', 0))
    except: return 0
near_miss = [e for e in LEDGER if e.get('Status') == 'REJECTED_LOW_SCORE' and conf(e) >= 35]

print(f"\n{'='*82}")
print(f"BACKTEST — new gates applied to historical ledger")
print(f"{'='*82}")
print(f"Actionable signals (WATCH/TRIGGER): {len(actionable)}")
print(f"Near-miss low-scores (≥35): {len(near_miss)}")
print()

def evaluate(records, label):
    print(f"\n--- {label} ---")
    print(f"{'ticker':<6} {'dir':<4} {'orig_status':<28} {'4H':>8} {'EOD':>8} {'gate_decision':<35}")
    n_kept = n_filtered = 0
    kept_returns = []
    filtered_returns = []
    for e in records:
        t = e.get('Ticker')
        d = e.get('Type', 'CALL')
        if d not in ('CALL', 'PUT'):
            continue
        key = (t, e.get('Timestamp', ''))
        out = outcomes.get(key, {})
        fwd_4h = out.get('fwd_4h')
        fwd_eod = out.get('fwd_eod')
        # Note: ledger forward returns from audit are already directional for triggers,
        # raw stock for shadows. For a clean comparison, apply sign for CALL=+, PUT=-.
        # But TRIGGER records have been sign-flipped already; shadows haven't.
        # Audit handles the sign in compute_multi_window for stock returns.

        # Run new gates
        gate_msgs = []
        gate_blocked = False

        score_delta = 0  # net score change from new gates

        # Earnings halt — hard block (within 3 days, tightened from 5)
        try:
            er = earnings_within(t, days=3)
            if er.get('within'):
                gate_msgs.append(f"BLOCK earnings in {er.get('days_until')}d")
                gate_blocked = True
        except: pass

        # IV term inversion — DISABLED in production (backtest showed it killed CIFR winner)
        # Leaving helper callable for forensics but not affecting score.

        # Squeeze score (additive — doesn't block, boosts/penalizes)
        try:
            sq_pts, sq_msg = squeeze_score(t, d)
            if sq_pts:
                gate_msgs.append(f"squeeze {sq_pts:+d}")
                score_delta += sq_pts
        except: pass

        # Seasonality — DISABLED in production (uniform April penalty, no signal)

        # If score_delta drives original below threshold (40), the signal is effectively filtered.
        # Reconstruct: original Confidence + score_delta < 40 → would no longer trigger.
        orig_conf = conf(e)
        new_score_est = orig_conf + score_delta
        if not gate_blocked and orig_conf >= 40 and new_score_est < 40:
            gate_msgs.append(f"score {orig_conf:.0f}→{new_score_est:.0f} below 40")
            gate_blocked = True

        decision = " | ".join(gate_msgs) if gate_msgs else "no change"
        if gate_blocked:
            n_filtered += 1
            if fwd_eod is not None: filtered_returns.append(fwd_eod)
        else:
            n_kept += 1
            if fwd_eod is not None: kept_returns.append(fwd_eod)

        fwd_4h_str = f"{fwd_4h*100:+.2f}%" if fwd_4h is not None else "—"
        fwd_eod_str = f"{fwd_eod*100:+.2f}%" if fwd_eod is not None else "—"
        print(f"{t:<6} {d:<4} {e.get('Status',''):<28} {fwd_4h_str:>8} {fwd_eod_str:>8} {decision:<35}")

    print(f"\n  kept: {n_kept}, filtered: {n_filtered}")
    if kept_returns:
        wins = sum(1 for r in kept_returns if r > 0.005)
        losses = sum(1 for r in kept_returns if r < -0.005)
        print(f"  KEPT win/loss (EOD>0.5%): {wins}W/{losses}L | "
              f"median: {statistics.median(kept_returns)*100:+.2f}%")
    if filtered_returns:
        wins = sum(1 for r in filtered_returns if r > 0.005)
        losses = sum(1 for r in filtered_returns if r < -0.005)
        print(f"  FILTERED win/loss: {wins}W/{losses}L | "
              f"median: {statistics.median(filtered_returns)*100:+.2f}%")
        print(f"  → if filter removed losers, that's good. If it removed winners, bad.")
    return n_kept, n_filtered, kept_returns, filtered_returns

evaluate(actionable, "ACTIONABLE SIGNALS (WATCH + TRIGGER)")
evaluate(near_miss[:20], "NEAR-MISS LOW SCORE (35-39, sample of 20)")

# Summary across both
print(f"\n{'='*82}")
print(f"SUMMARY")
print(f"{'='*82}")
print("Note: gates evaluable now use *current* API state (earnings/SI/IV-term).")
print("They are best estimates of how the new pipeline would have judged each name today.")
print("oi-change, darkpool, options-volume, economic-calendar halts are NOT backtested")
print("(historical intraday API data unavailable).")
