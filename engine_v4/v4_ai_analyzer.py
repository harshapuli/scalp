"""
V4 ELITE FORENSICS DOSSIER & SHADOW AUDIT (v2)

What changed vs v1:
- Fixed: MFE/MAE returned 0.0 for every event because Alpaca 15Min bars only
  exist during RTH. After-hours rejections produced empty dataframes. We now
  advance the measurement window to the next regular session open and use
  1Hour bars consistently.
- Added: multi-window forward returns (1H, 4H, EOD, next-day) so each filter's
  edge can be evaluated at the horizon it's actually predicting.
- Added: SPY baseline subtraction so a "rejected ticker fell 0.5%" is judged
  against the broader tape, not in isolation.
- Added: per-filter aggregation with directional verdicts (validated /
  over-gating / unresolved) and median-based statistics so a single outlier
  print doesn't move the read.
- Aware: new statuses WATCH_*, TRIGGER_*_CONFIRMED, WATCH_EXPIRED.
"""
import os
import sys
import json
import statistics
from datetime import datetime, timedelta, time as dtime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

# Regular trading hours in UTC (US equities, post-DST agnostic — using EDT base 13:30Z–20:00Z)
RTH_OPEN_UTC = dtime(13, 30)
RTH_CLOSE_UTC = dtime(20, 0)
HORIZONS = ['1H', '4H', 'EOD', 'D1']  # measurement windows
DEFAULT_RETURN = {h: None for h in HORIZONS}

TRIGGER_STATUSES = {'TRIGGER_BREAKOUT', 'TRIGGER_PULLBACK',
                    'TRIGGER_BREAKOUT_CONFIRMED', 'TRIGGER_PULLBACK_CONFIRMED'}
WATCH_STATUSES = {'WATCH_BREAKOUT', 'WATCH_PULLBACK', 'WATCH_EXPIRED'}


def next_rth_open(t_utc: datetime) -> datetime:
    """Advance a UTC timestamp to the next regular session open if it's outside RTH or on a weekend."""
    candidate = t_utc
    for _ in range(5):  # at most skip a long weekend
        wd = candidate.weekday()  # Mon=0..Sun=6
        if wd >= 5:
            # Saturday/Sunday → jump to Monday open
            candidate = (candidate + timedelta(days=(7 - wd))).replace(
                hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
            continue
        t_only = candidate.time()
        if t_only < RTH_OPEN_UTC:
            return candidate.replace(hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
        if t_only >= RTH_CLOSE_UTC:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=RTH_OPEN_UTC.hour, minute=RTH_OPEN_UTC.minute, second=0, microsecond=0)
            continue
        return candidate
    return t_utc  # fallback


def fetch_returns_window(ticker: str, start: datetime, hours_forward: int) -> tuple:
    """Pull 1H bars from start to start + hours_forward. Return (entry_price, mfe%, mae%, fwd_ret%)
    with returns measured on the underlying. Returns (0,0,0,None) if no data."""
    end = start + timedelta(hours=hours_forward)
    df = fetch_alpaca_bars(ticker, '1Hour',
                           start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           end.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty:
        return 0.0, 0.0, 0.0, None
    entry = float(df['Open'].iloc[0])
    if entry <= 0:
        return 0.0, 0.0, 0.0, None
    high = float(df['High'].max())
    low = float(df['Low'].min())
    last = float(df['Close'].iloc[-1])
    mfe = (high - entry) / entry
    mae = (low - entry) / entry
    fwd = (last - entry) / entry
    return entry, mfe, mae, fwd


def compute_multi_window(ticker: str, trade_type: str, entry_ts: str) -> dict:
    """Compute MFE/MAE/forward returns across 1H/4H/EOD/D1 windows + SPY baselines.
    Sign-flips for PUTs so positive = good for the trade direction."""
    out = {
        'entry_price': 0.0, 'measurement_start_utc': None,
        'mfe': dict(DEFAULT_RETURN), 'mae': dict(DEFAULT_RETURN),
        'fwd_ret': dict(DEFAULT_RETURN), 'spy_fwd_ret': dict(DEFAULT_RETURN),
        'excess_ret': dict(DEFAULT_RETURN), 'data_status': 'OK'
    }
    try:
        ts = datetime.strptime(entry_ts, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        out['data_status'] = 'BAD_TIMESTAMP'
        return out

    measurement_start = next_rth_open(ts)
    out['measurement_start_utc'] = measurement_start.strftime('%Y-%m-%dT%H:%M:%SZ')

    # If measurement start is in the future (analyzer ran before next session) bail gracefully
    if measurement_start > datetime.utcnow():
        out['data_status'] = 'PENDING_NEXT_SESSION'
        return out

    horizon_hours = {'1H': 1, '4H': 4, 'EOD': 7, 'D1': 24}
    # CALL or UNKNOWN → sign=+1 (interpret raw underlying direction); PUT → flip.
    # UNKNOWN happens for early Phase-1 rejections where flow type wasn't determined.
    sign = -1 if trade_type == 'PUT' else 1

    for h, hrs in horizon_hours.items():
        # Need full window elapsed before measuring
        if datetime.utcnow() < measurement_start + timedelta(hours=hrs):
            continue
        entry, mfe, mae, fwd = fetch_returns_window(ticker, measurement_start, hrs)
        if fwd is None:
            continue
        if h == '1H':
            out['entry_price'] = entry
        # Apply sign convention so + means the trade thesis worked
        out['mfe'][h] = (mfe if sign > 0 else -mae)
        out['mae'][h] = (mae if sign > 0 else -mfe)
        out['fwd_ret'][h] = sign * fwd

        _, _, _, spy_fwd = fetch_returns_window('SPY', measurement_start, hrs)
        if spy_fwd is not None:
            out['spy_fwd_ret'][h] = sign * spy_fwd
            out['excess_ret'][h] = out['fwd_ret'][h] - out['spy_fwd_ret'][h]

    return out


def diagnostic_statement(trade: dict, m: dict) -> str:
    """Human-readable forensic line for one ledger entry."""
    status = trade.get('Status', '')
    scr_logic = trade.get('Screener_Logic', '—')

    # Pick the most informative window we have data for
    fwd_4h = m['fwd_ret'].get('4H')
    fwd_eod = m['fwd_ret'].get('EOD')
    fwd_d1 = m['fwd_ret'].get('D1')
    excess_4h = m['excess_ret'].get('4H')
    primary = next((x for x in (fwd_4h, fwd_eod, fwd_d1) if x is not None), None)

    if m['data_status'] == 'PENDING_NEXT_SESSION':
        action_line = f"⏳ AWAITING DATA: rejection at {trade.get('Timestamp', '')[:16]} fell after RTH; measuring from next open."
    elif primary is None:
        action_line = f"⏳ AWAITING DATA: {m['data_status']} (window not yet elapsed)."
    elif status in TRIGGER_STATUSES:
        if primary > 0.005:
            action_line = f"🟢 TARGET HIT: +{primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
        elif primary < -0.005:
            action_line = f"🔴 LOSS: {primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
        else:
            action_line = f"⚪ FLAT: {primary*100:.2f}% (excess vs SPY: {excess_4h*100 if excess_4h is not None else 0:+.2f}%)"
    elif status.startswith('REJECTED_'):
        # For rejections: positive fwd means filter MISSED a winner; negative means it AVOIDED a loser
        if excess_4h is not None and excess_4h <= -0.005:
            action_line = f"🛡️ FILTER VALIDATED: ticker underperformed SPY by {-excess_4h*100:.2f}% post-rejection."
        elif excess_4h is not None and excess_4h >= 0.01:
            action_line = f"⚠️ OVER-FILTERED: ticker outperformed SPY by +{excess_4h*100:.2f}% post-rejection."
        else:
            action_line = f"⚪ NEUTRAL: ticker tracked SPY ({primary*100:+.2f}% raw, excess {excess_4h*100 if excess_4h is not None else 0:+.2f}%)."
    elif status in WATCH_STATUSES:
        action_line = f"👁️ WATCH state ({status}); no entry executed."
    else:
        action_line = f"— {status}"

    horizons_str = " | ".join(f"{h}:{m['fwd_ret'][h]*100:+.2f}%" if m['fwd_ret'][h] is not None else f"{h}:—" for h in HORIZONS)

    return (f"[{trade['Ticker']} {trade.get('Type','?')}] {status}\n"
            f"   Screener     : {scr_logic}\n"
            f"   Forward (raw): {horizons_str}\n"
            f"   Verdict      : {action_line}")


def aggregate_filter_edge(rejection_records: list) -> dict:
    """Group rejections by filter, compute median excess return at 4H+EOD+D1, return verdict."""
    by_filter = {}
    for r in rejection_records:
        f = r['filter']
        by_filter.setdefault(f, {'excess_4h': [], 'excess_eod': [], 'excess_d1': [], 'count': 0})
        by_filter[f]['count'] += 1
        if r['excess'].get('4H') is not None: by_filter[f]['excess_4h'].append(r['excess']['4H'])
        if r['excess'].get('EOD') is not None: by_filter[f]['excess_eod'].append(r['excess']['EOD'])
        if r['excess'].get('D1') is not None: by_filter[f]['excess_d1'].append(r['excess']['D1'])

    out = {}
    for f, d in by_filter.items():
        med_4h = statistics.median(d['excess_4h']) if d['excess_4h'] else None
        med_eod = statistics.median(d['excess_eod']) if d['excess_eod'] else None
        med_d1 = statistics.median(d['excess_d1']) if d['excess_d1'] else None
        # Use the longest-horizon median that has >= 5 samples for the verdict
        chosen = None; chosen_label = None; chosen_n = 0
        for label, vals in (('D1', d['excess_d1']), ('EOD', d['excess_eod']), ('4H', d['excess_4h'])):
            if len(vals) >= 5:
                chosen = statistics.median(vals); chosen_label = label; chosen_n = len(vals); break
        if chosen is None:
            verdict = 'INSUFFICIENT_DATA'
        elif chosen <= -0.005:
            verdict = 'VALIDATED'
        elif chosen >= 0.005:
            verdict = 'OVER_GATING'
        else:
            verdict = 'NEUTRAL'
        out[f] = {
            'count': d['count'],
            'median_excess_4h': med_4h, 'median_excess_eod': med_eod, 'median_excess_d1': med_d1,
            'verdict': verdict, 'verdict_horizon': chosen_label, 'verdict_n': chosen_n,
        }
    return out


def run_daily_analysis():
    ledger_path = os.path.join(os.path.dirname(__file__), 'v4_ledger.json')
    shadow_path = os.path.join(os.path.dirname(__file__), 'v4_rejection_audit.json')

    if not os.path.exists(ledger_path):
        print("No trades logged in ledger.")
        return
    with open(ledger_path, 'r') as f:
        ledger = json.load(f)
    if not ledger:
        print("Ledger is empty.")
        return

    print("\n" + "=" * 80)
    print("🧠 V4 ELITE FORENSICS DOSSIER & SHADOW AUDIT (v2)")
    print("=" * 80 + "\n")

    triggers = []
    rejection_records = []  # for filter aggregation
    shadow_audit = []       # for JSON output

    for trade in ledger:
        m = compute_multi_window(trade['Ticker'], trade.get('Type', 'CALL'), trade.get('Timestamp', ''))
        statement = diagnostic_statement(trade, m)
        status = trade.get('Status', '')

        record = {
            'Ticker': trade['Ticker'], 'Type': trade.get('Type', '?'), 'Status': status,
            'Timestamp': trade.get('Timestamp'), 'Date': trade.get('Date'),
            'Confidence': trade.get('Confidence'),
            'Measurement_Start_UTC': m['measurement_start_utc'],
            'Entry_Price': m['entry_price'],
            'MFE': m['mfe'], 'MAE': m['mae'],
            'Fwd_Ret': m['fwd_ret'], 'SPY_Fwd_Ret': m['spy_fwd_ret'],
            'Excess_Ret': m['excess_ret'],
            'Data_Status': m['data_status'],
        }

        if status in TRIGGER_STATUSES:
            triggers.append(record)
            print(statement); print("-" * 80)
        elif status.startswith('REJECTED_'):
            rejection_records.append({'filter': status, 'excess': m['excess_ret']})
            shadow_audit.append(record)
            print(statement); print("-" * 80)
        # WATCH_* statuses: log but don't analyze edge (no decision was taken)

    # Trigger aggregate
    if triggers:
        wins = sum(1 for t in triggers if t['Fwd_Ret'].get('EOD') is not None and t['Fwd_Ret']['EOD'] > 0.005)
        losses = sum(1 for t in triggers if t['Fwd_Ret'].get('EOD') is not None and t['Fwd_Ret']['EOD'] < -0.005)
        n = wins + losses
        wr = (wins / n * 100) if n else 0
        print(f"\n📊 TRIGGER SUMMARY (EOD horizon, n={n}): {wins}W / {losses}L → {wr:.1f}% win rate")
    else:
        print("\n📊 TRIGGER SUMMARY: no triggers in ledger yet.")

    # Filter edge aggregation
    edge = aggregate_filter_edge(rejection_records)
    print("\n⚖️ REJECTION SHADOW BOOK — per-filter median excess return vs SPY")
    if not edge:
        print("  > No rejection data yet.")
    for f, d in edge.items():
        med = d.get('median_excess_4h')
        med_str = f"{med*100:+.2f}%" if med is not None else "—"
        verdict = d['verdict']
        if verdict == 'VALIDATED':
            tag = '🟢 VALIDATED'
        elif verdict == 'OVER_GATING':
            tag = '🔴 OVER-GATING'
        elif verdict == 'NEUTRAL':
            tag = '⚪ NEUTRAL'
        else:
            tag = '⏳ INSUFFICIENT (need 5+ samples)'
        h_used = d.get('verdict_horizon') or '—'
        n_used = d.get('verdict_n') or 0
        print(f"  > {tag} | {f} (n={d['count']}, verdict_n={n_used}@{h_used}) | excess_4h_median={med_str}")

    # Persist
    with open(shadow_path, 'w') as f:
        json.dump({
            'generated_utc': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'filter_edge': edge,
            'trigger_records': triggers,
            'shadow_records': shadow_audit,
        }, f, indent=2, default=str)
    print(f"\n💾 Shadow audit written to {shadow_path}")


if __name__ == "__main__":
    run_daily_analysis()
