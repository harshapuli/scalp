"""
V4 PAPER TRADING JOURNAL — append-only event log with forward returns.

Captures every signal lifecycle event from the V4 stack and computes outcomes
at 1H / 4H / EOD / D+1 / D+3 horizons vs SPY baseline. Output is a clean CSV
you can open in Excel/Numbers for weekly review.

DESIGNED FOR PAPER TRADING: false signals are valuable data, not failures.
The journal answers questions like:
  - "Of all WATCH_PULLBACK events, what % became profitable trades?"
  - "Does score >= 60 produce different outcomes than 40-50?"
  - "Which rejection types saved capital vs over-filtered?"
  - "Do TRIGGERED pre-position candidates beat SPY by enough to justify size?"

EVENTS CAPTURED:
  - From v4_ledger.json: TRIGGER_*_CONFIRMED, WATCH_*, WATCH_EXPIRED, REJECTED_*
  - From v4_patrol_log.jsonl: PATROL_PREPOSITION_TRANSITION, PATROL_INVALIDATION,
    PATROL_FAST_RETEST, PATROL_ENGINE_STALE

OUTPUTS:
  - v4_paper_journal.csv  — one row per event, opens in Excel
  - v4_paper_journal.jsonl — full event details for deep analysis
  - v4_paper_journal_state.json — dedup state (last processed event keys)

RUN: python3 engine_v4/v4_paper_journal.py
FREQUENCY: Daily after close, or on demand. Safe to run multiple times — dedup
prevents duplicate rows. Re-runs also retroactively fill in outcomes for events
whose forward windows have now elapsed.
"""
import os
import sys
import csv
import json
import hashlib
from datetime import datetime, timedelta, timezone, time as dtime

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LEDGER_PATH = os.path.join(BASE_DIR, 'v4_ledger.json')
PATROL_LOG_PATH = os.path.join(BASE_DIR, 'v4_patrol_log.jsonl')
PREPOSITION_PATH = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')

JOURNAL_CSV = os.path.join(BASE_DIR, 'v4_paper_journal.csv')
JOURNAL_JSONL = os.path.join(BASE_DIR, 'v4_paper_journal.jsonl')
JOURNAL_STATE = os.path.join(BASE_DIR, 'v4_paper_journal_state.json')

# RTH bounds for forward-return windows
RTH_OPEN = dtime(13, 30)
RTH_CLOSE = dtime(20, 0)

CSV_COLUMNS = [
    'event_id', 'date_utc', 'time_utc', 'ticker', 'direction', 'event_type',
    'score', 'path', 'dte',
    'entry_price', 'spy_at_event',
    'ret_1h_pct', 'ret_4h_pct', 'ret_eod_pct', 'ret_d1_pct', 'ret_d3_pct',
    'spy_ret_eod_pct', 'excess_eod_pct',
    'mfe_eod_pct', 'mae_eod_pct',
    'factor_summary', 'screener_logic',
]


# ---------- Helpers ----------
def event_id(*parts):
    """Stable hash for dedup."""
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def parse_ts(ts_str):
    if not ts_str: return None
    try:
        s = ts_str[:19]
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)
    except Exception:
        return None


def next_rth_open(t_utc):
    """Advance after-hours/weekend timestamps to next regular session open."""
    candidate = t_utc
    for _ in range(5):
        wd = candidate.weekday()
        if wd >= 5:
            candidate = (candidate + timedelta(days=(7 - wd))).replace(
                hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
            continue
        t_only = candidate.time()
        if t_only < RTH_OPEN:
            return candidate.replace(hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
        if t_only >= RTH_CLOSE:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
            continue
        return candidate
    return t_utc


def fwd_return(ticker, start_dt, hours_forward, sign=1):
    """Pull bars from start_dt to start_dt + hours_forward. Return (entry, fwd%, mfe%, mae%) or None."""
    end_dt = start_dt + timedelta(hours=hours_forward)
    df = fetch_alpaca_bars(ticker, '1Hour',
                           start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           end_dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty: return None
    entry = float(df['Open'].iloc[0])
    if entry <= 0: return None
    last = float(df['Close'].iloc[-1])
    high = float(df['High'].max())
    low = float(df['Low'].min())
    if sign > 0:
        return entry, (last - entry) / entry, (high - entry) / entry, (low - entry) / entry
    else:
        # PUT: profit when price falls — flip signs
        return entry, (entry - last) / entry, (entry - low) / entry, (entry - high) / entry


def compute_outcomes(ticker, ts_utc, direction):
    """Compute forward returns at multiple horizons. Returns dict."""
    if not ts_utc:
        return {}
    measurement_start = next_rth_open(ts_utc)
    if measurement_start > datetime.now(UTC):
        return {'_pending': True}

    sign = -1 if direction == 'PUT' else 1
    horizons = {'1h': 1, '4h': 4, 'eod': 7, 'd1': 24, 'd3': 72}
    out = {'measurement_start': measurement_start.strftime('%Y-%m-%dT%H:%M:%SZ')}
    entry_price = None

    for label, hrs in horizons.items():
        if datetime.now(UTC) < measurement_start + timedelta(hours=hrs):
            continue
        r = fwd_return(ticker, measurement_start, hrs, sign)
        if r is None: continue
        entry, fwd, mfe, mae = r
        out[f'ret_{label}_pct'] = round(fwd * 100, 3)
        if label == 'eod':
            out['mfe_eod_pct'] = round(mfe * 100, 3)
            out['mae_eod_pct'] = round(mae * 100, 3)
            out['entry_price'] = round(entry, 2)
        elif label == '1h' and entry_price is None:
            out['entry_price'] = round(entry, 2)
            entry_price = entry

    # SPY baseline + excess
    if 'ret_eod_pct' in out:
        spy_r = fwd_return('SPY', measurement_start, 7, sign=1)  # SPY always raw direction
        if spy_r:
            spy_fwd = spy_r[1] * 100
            out['spy_ret_eod_pct'] = round(spy_fwd, 3)
            out['excess_eod_pct'] = round(out['ret_eod_pct'] - sign * spy_fwd, 3)

    return out


# ---------- Event extraction ----------
def extract_ledger_events():
    """Read v4_ledger.json → list of normalized events."""
    if not os.path.exists(LEDGER_PATH): return []
    try:
        with open(LEDGER_PATH) as f:
            ledger = json.load(f)
    except Exception:
        return []
    out = []
    for entry in ledger:
        ts = entry.get('Timestamp', '')
        ts_dt = parse_ts(ts)
        if not ts_dt: continue
        eid = event_id('ledger', entry.get('Ticker'), entry.get('Status'), ts)
        ep = entry.get('Exit_Protocol') or {}
        sm = entry.get('Score_Matrix') or {}
        factor = "|".join(f"{k}:{v}" for k, v in sm.items()) if sm else ''
        out.append({
            'event_id': eid,
            'ts_utc': ts_dt,
            'ticker': entry.get('Ticker', '?'),
            'direction': entry.get('Type', 'UNKNOWN'),
            'event_type': entry.get('Status', 'UNKNOWN'),
            'score': entry.get('Confidence', '0.0'),
            'path': ep.get('Path', ''),
            'dte': entry.get('DTE', 0),
            'factor_summary': factor,
            'screener_logic': entry.get('Screener_Logic', ''),
            'raw': entry,
        })
    return out


def extract_patrol_events():
    """Read v4_patrol_log.jsonl → list of normalized events."""
    if not os.path.exists(PATROL_LOG_PATH): return []
    out = []
    try:
        with open(PATROL_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    e = json.loads(line)
                except: continue
                ts_str = e.get('ts_utc', '')
                ts_dt = parse_ts(ts_str)
                if not ts_dt: continue
                ev_type = e.get('type', 'PATROL_UNKNOWN')
                ticker = e.get('ticker', '?')
                eid = event_id('patrol', ev_type, ticker, ts_str)
                direction = 'CALL'
                if e.get('direction'):
                    direction = e['direction']
                elif e.get('watch_type'):
                    direction = e['watch_type']
                out.append({
                    'event_id': eid,
                    'ts_utc': ts_dt,
                    'ticker': ticker,
                    'direction': direction,
                    'event_type': ev_type,
                    'score': '',
                    'path': e.get('watch_path', ''),
                    'dte': '',
                    'factor_summary': f"transition:{e.get('from_status','')}_to_{e.get('to_status','')}|"
                                       f"trigger:{e.get('trigger','')}|spot:{e.get('spot','')}|"
                                       f"reason:{e.get('reason', e.get('pattern',''))}",
                    'screener_logic': '',
                    'raw': e,
                })
    except Exception as ex:
        print(f"⚠️ patrol log read error: {ex}")
    return out


# ---------- State management ----------
def load_state():
    if os.path.exists(JOURNAL_STATE):
        try:
            with open(JOURNAL_STATE) as f:
                return json.load(f)
        except Exception: pass
    return {'processed_ids': []}


def save_state(state):
    with open(JOURNAL_STATE, 'w') as f:
        json.dump(state, f, indent=2)


def load_existing_csv_event_ids():
    """For dedup — read existing CSV (if any) and collect event_ids that already have all outcome cols filled."""
    if not os.path.exists(JOURNAL_CSV): return set()
    finalized = set()
    try:
        with open(JOURNAL_CSV) as f:
            reader = csv.DictReader(f)
            for row in reader:
                # An event is "finalized" once we have d3 outcomes (3 days past entry)
                if row.get('ret_d3_pct') and row.get('ret_d3_pct') != '':
                    finalized.add(row['event_id'])
    except Exception: pass
    return finalized


def append_csv_rows(rows):
    """Append new rows. If file doesn't exist, write header first."""
    file_exists = os.path.exists(JOURNAL_CSV)
    with open(JOURNAL_CSV, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_jsonl_rows(rows):
    with open(JOURNAL_JSONL, 'a') as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


# ---------- Main ----------
def main():
    print(f"\n{'='*70}")
    print(f"V4 PAPER TRADING JOURNAL")
    print(f"{'='*70}")

    finalized_ids = load_existing_csv_event_ids()
    print(f"Existing finalized rows: {len(finalized_ids)}")

    ledger_events = extract_ledger_events()
    patrol_events = extract_patrol_events()
    all_events = ledger_events + patrol_events
    print(f"Events found — ledger: {len(ledger_events)}, patrol: {len(patrol_events)}, total: {len(all_events)}")

    if not all_events:
        print("No events to process.")
        return

    # Dedup against finalized rows; skip already-fully-resolved events
    to_process = [e for e in all_events if e['event_id'] not in finalized_ids]
    print(f"To process (new or pending outcomes): {len(to_process)}")

    if not to_process:
        print("Nothing new. Journal is up to date.")
        return

    # Sort by timestamp (chronological)
    to_process.sort(key=lambda e: e['ts_utc'])

    # If we're updating already-existing rows (re-runs to add late outcomes), we need a strategy:
    # Simplest: rebuild the CSV from scratch each run if there are late updates.
    # That gives us deterministic state and avoids partial-row complications.
    print("\nComputing outcomes for each event (1H / 4H / EOD / D+1 / D+3 vs SPY)...")
    rows_csv = []
    rows_jsonl = []

    for i, e in enumerate(to_process):
        outcomes = compute_outcomes(e['ticker'], e['ts_utc'], e['direction'])
        row = {
            'event_id': e['event_id'],
            'date_utc': e['ts_utc'].strftime('%Y-%m-%d'),
            'time_utc': e['ts_utc'].strftime('%H:%M:%S'),
            'ticker': e['ticker'],
            'direction': e['direction'],
            'event_type': e['event_type'],
            'score': e['score'],
            'path': e['path'],
            'dte': e['dte'],
            'entry_price': outcomes.get('entry_price', ''),
            'spy_at_event': '',  # filled implicitly via spy_ret
            'ret_1h_pct': outcomes.get('ret_1h_pct', ''),
            'ret_4h_pct': outcomes.get('ret_4h_pct', ''),
            'ret_eod_pct': outcomes.get('ret_eod_pct', ''),
            'ret_d1_pct': outcomes.get('ret_d1_pct', ''),
            'ret_d3_pct': outcomes.get('ret_d3_pct', ''),
            'spy_ret_eod_pct': outcomes.get('spy_ret_eod_pct', ''),
            'excess_eod_pct': outcomes.get('excess_eod_pct', ''),
            'mfe_eod_pct': outcomes.get('mfe_eod_pct', ''),
            'mae_eod_pct': outcomes.get('mae_eod_pct', ''),
            'factor_summary': e.get('factor_summary', ''),
            'screener_logic': e.get('screener_logic', ''),
        }
        rows_csv.append(row)
        rows_jsonl.append({**row, 'raw': e['raw']})

        if (i + 1) % 10 == 0:
            print(f"  ...processed {i+1}/{len(to_process)}")

    # Strategy: rebuild CSV from scratch (idempotent, handles late-arriving outcomes)
    if os.path.exists(JOURNAL_CSV):
        # Read existing rows we haven't reprocessed
        existing_rows = []
        try:
            with open(JOURNAL_CSV) as f:
                reader = csv.DictReader(f)
                existing_rows = [r for r in reader if r['event_id'] in finalized_ids]
        except Exception: pass
        # Rewrite: existing finalized + new
        with open(JOURNAL_CSV, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
            writer.writeheader()
            for r in existing_rows:
                writer.writerow(r)
            for r in rows_csv:
                writer.writerow(r)
    else:
        with open(JOURNAL_CSV, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
            writer.writeheader()
            for r in rows_csv:
                writer.writerow(r)

    # JSONL is append-only — let it grow (full audit trail)
    append_jsonl_rows(rows_jsonl)

    # Save state
    state = {'processed_ids': list(finalized_ids | {r['event_id'] for r in rows_csv if r['ret_d3_pct']})}
    save_state(state)

    # ---------- Summary ----------
    print(f"\n{'='*70}")
    print("JOURNAL UPDATED")
    print(f"{'='*70}")
    print(f"  Total rows in CSV: {len(rows_csv) + len(finalized_ids)}")
    print(f"  Newly processed: {len(rows_csv)}")
    print(f"  Pending outcomes (will fill on re-run): "
          f"{sum(1 for r in rows_csv if not r['ret_d3_pct'])}")

    # Quick aggregate: win rate by event type
    by_type = {}
    for r in rows_csv:
        t = r['event_type']
        by_type.setdefault(t, []).append(r)

    print(f"\nBy event type:")
    for t, rows in sorted(by_type.items(), key=lambda x: -len(x[1])):
        eod_returns = [float(r['ret_eod_pct']) for r in rows if r['ret_eod_pct']]
        excess = [float(r['excess_eod_pct']) for r in rows if r['excess_eod_pct']]
        if eod_returns:
            wins = sum(1 for x in eod_returns if x > 0.5)
            losses = sum(1 for x in eod_returns if x < -0.5)
            avg = sum(eod_returns) / len(eod_returns)
            avg_ex = sum(excess) / len(excess) if excess else 0
            print(f"  {t:<35} n={len(rows):>3}  W/L: {wins}/{losses}  avg_eod={avg:+.2f}%  vs_SPY={avg_ex:+.2f}%")
        else:
            print(f"  {t:<35} n={len(rows):>3}  (no outcomes yet)")

    print(f"\n💾 CSV:   {JOURNAL_CSV}")
    print(f"💾 JSONL: {JOURNAL_JSONL}")
    print(f"\nWeekly review: open the CSV in Excel/Numbers, filter by event_type, sort by score, look for patterns.")


if __name__ == "__main__":
    main()
