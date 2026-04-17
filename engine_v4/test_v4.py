"""
V4 ENGINE TEST SUITE — focused on what we built today.

Coverage:
  - TP target computation (DTE-scaled multipliers)
  - Premium scoring (log-scale + ask_dominance penalty)
  - Watch helpers (age, expiry by DTE bucket)
  - RTH window logic (in_window, weekend handling, boundaries)
  - Patrol invalidation (FVG breach for pullback, failed breakout)
  - Patrol fast retest (1-min rejection wick / breakout body confirm)
  - Engine health check (signals.json staleness)

Run:
  python3 engine_v4/test_v4.py
"""
import os
import sys
import json
import math
import tempfile
import shutil
from datetime import datetime, timedelta, time as dtime, timezone

# Add engine_v4 + parent to path
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pandas as pd

# Imports from modules under test
from v4_meta_engine import (
    compute_tp_target, tp_premium_multiplier,
    watch_age_hours, watch_expired,
)
from v4_patrol_engine import (
    check_invalidation, check_fast_retest, check_engine_health,
    in_window as patrol_in_window,
    INVALIDATION_BUFFER, STALE_ENGINE_THRESHOLD_SEC,
)
from v4_armada import in_scan_window, next_window_open, PACIFIC

UTC = timezone.utc

# ---------- Tiny test harness (no pytest dependency) ----------
class T:
    passed = 0
    failed = 0
    failures = []

def test(name):
    def deco(fn):
        try:
            fn()
            T.passed += 1
            print(f"  ✓ {name}")
        except AssertionError as e:
            T.failed += 1
            T.failures.append((name, str(e)))
            print(f"  ✗ {name}: {e}")
        except Exception as e:
            T.failed += 1
            T.failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
        return fn
    return deco

def assert_eq(actual, expected, msg=""):
    assert actual == expected, f"{msg} expected {expected!r}, got {actual!r}"

def assert_close(actual, expected, tol=0.01, msg=""):
    assert abs(actual - expected) <= tol, f"{msg} expected ~{expected}, got {actual}"

def assert_in(item, container, msg=""):
    assert item in container, f"{msg} {item!r} not in {container!r}"

# ---------- Helpers for building synthetic intraday bars ----------
def make_bars(rows, base_ts=None):
    """rows = [(open, high, low, close, volume), ...] starting from base_ts (UTC, tz-aware)
    indexed at 1-min intervals."""
    if base_ts is None:
        base_ts = datetime(2026, 4, 17, 14, 0, tzinfo=UTC)
    idx = pd.DatetimeIndex([base_ts + timedelta(minutes=i) for i in range(len(rows))], tz='UTC')
    df = pd.DataFrame(rows, columns=['Open', 'High', 'Low', 'Close', 'Volume'], index=idx)
    return df


# ===========================================================
# 1. TP TARGET COMPUTATION
# ===========================================================
print("\n[1/7] TP target computation")

@test("0-7 DTE uses 1.30x multiplier")
def _():
    assert_eq(tp_premium_multiplier(5), 1.30)
    assert_eq(tp_premium_multiplier(7), 1.30)
    r = compute_tp_target(3.80, 7)
    assert_close(r['target_premium'], 4.94, 0.01)
    assert_eq(r['pct_gain'], 30)

@test("8-21 DTE uses 1.50x multiplier")
def _():
    assert_eq(tp_premium_multiplier(14), 1.50)
    assert_eq(tp_premium_multiplier(21), 1.50)
    r = compute_tp_target(5.20, 14)
    assert_close(r['target_premium'], 7.80, 0.01)
    assert_eq(r['pct_gain'], 50)

@test("22+ DTE uses 1.80x multiplier")
def _():
    assert_eq(tp_premium_multiplier(30), 1.80)
    r = compute_tp_target(12.00, 30)
    assert_close(r['target_premium'], 21.60, 0.01)
    assert_eq(r['pct_gain'], 80)

@test("Zero or negative entry premium returns N/A")
def _():
    r = compute_tp_target(0, 7)
    assert_eq(r['target_premium'], None)
    assert_in("N/A", r['display'])
    r2 = compute_tp_target(-1.5, 7)
    assert_eq(r2['target_premium'], None)

@test("Display string contains both target and pct")
def _():
    r = compute_tp_target(10.00, 14)
    assert_in("$15.00", r['display'])
    assert_in("+50%", r['display'])
    assert_in("$10.00", r['display'])  # entry estimate


# ===========================================================
# 2. PREMIUM SCORING (log-scale + ask_dom penalty)
# ===========================================================
print("\n[2/7] Premium scoring (log-scale)")

def calc_prem_score(prem, sweep_count=1, ask_dom=1.0):
    """Mirror the engine's premium scoring inline."""
    score = min(20, max(0, math.log10(max(prem / 100_000, 1)) * 7))
    if prem >= 500_000 and sweep_count >= 3:
        score = min(30, score + 10)
    if ask_dom < 0.65:
        score = max(0, score - 8)
    return score

@test("$100K premium → score ~0")
def _():
    assert_close(calc_prem_score(100_000), 0, 0.5)

@test("$1M premium → score ~7")
def _():
    assert_close(calc_prem_score(1_000_000), 7, 0.5)

@test("$10M premium → score capped at 20")
def _():
    assert_close(calc_prem_score(10_000_000), 14, 0.5)
    assert_eq(calc_prem_score(100_000_000), 20)  # capped

@test("Persistence bonus stacks (>=$500K + >=3 sweeps adds +10)")
def _():
    base = calc_prem_score(1_000_000, sweep_count=1)
    bonus = calc_prem_score(1_000_000, sweep_count=3)
    assert_close(bonus - base, 10, 0.1)

@test("Weak ask_dominance (<65%) penalizes -8")
def _():
    # Use $10M so base score (~14) is well above the 8-point penalty (no clamp at 0)
    full = calc_prem_score(10_000_000, sweep_count=1, ask_dom=0.95)
    weak = calc_prem_score(10_000_000, sweep_count=1, ask_dom=0.55)
    assert_close(full - weak, 8, 0.1)

@test("Score never goes negative even with max penalty")
def _():
    assert calc_prem_score(100_000, sweep_count=1, ask_dom=0.30) >= 0


# ===========================================================
# 3. WATCH HELPERS
# ===========================================================
print("\n[3/7] Watch lifecycle helpers")

def make_watch(dte=7, age_hours=0):
    created = (datetime.utcnow() - timedelta(hours=age_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {"ticker": "X", "type": "CALL", "path": "PULLBACK", "dte": dte,
            "zone_low": 100, "zone_high": 102, "score": 50, "created_at": created}

@test("watch_age_hours returns approximately the age")
def _():
    w = make_watch(age_hours=2.5)
    assert_close(watch_age_hours(w), 2.5, 0.05)

@test("0-7 DTE watch expires after 1 hour")
def _():
    assert not watch_expired(make_watch(dte=7, age_hours=0.5))
    assert watch_expired(make_watch(dte=7, age_hours=1.5))

@test("8-14 DTE watch expires after 4 hours")
def _():
    assert not watch_expired(make_watch(dte=14, age_hours=3.0))
    assert watch_expired(make_watch(dte=14, age_hours=5.0))

@test("15+ DTE watch expires after 12 hours")
def _():
    assert not watch_expired(make_watch(dte=21, age_hours=10))
    assert watch_expired(make_watch(dte=21, age_hours=13))

@test("Malformed timestamp treated as expired (safe default)")
def _():
    w = {"ticker": "X", "dte": 7, "created_at": "garbage"}
    assert watch_expired(w)


# ===========================================================
# 4. RTH WINDOW LOGIC
# ===========================================================
print("\n[4/7] RTH window (Pacific Time)")

def pt(weekday_offset, h, m):
    """Build a tz-aware PT datetime for tests. weekday_offset 0=Mon based on real Mon."""
    # Anchor to a known Monday (2026-04-13 was a Monday)
    base = datetime(2026, 4, 13, 0, 0, tzinfo=PACIFIC)
    return base.replace(hour=h, minute=m) + timedelta(days=weekday_offset)

@test("Window open at 6:00 AM PT Monday → True (armada)")
def _():
    assert in_scan_window(pt(0, 6, 0))

@test("Window open at 1:30 PM PT Monday → True (armada)")
def _():
    assert in_scan_window(pt(0, 13, 30))

@test("5:59 AM PT Monday → False (1 min before open)")
def _():
    assert not in_scan_window(pt(0, 5, 59))

@test("1:31 PM PT Monday → False (1 min after close)")
def _():
    assert not in_scan_window(pt(0, 13, 31))

@test("Saturday during normal hours → False (weekend)")
def _():
    assert not in_scan_window(pt(5, 10, 0))  # Sat 10 AM PT
    assert not in_scan_window(pt(6, 10, 0))  # Sun 10 AM PT

@test("Patrol uses same window as armada")
def _():
    assert patrol_in_window(pt(0, 8, 0))     # Mon 8 AM
    assert not patrol_in_window(pt(0, 14, 0))  # Mon 2 PM (closed)
    assert not patrol_in_window(pt(5, 8, 0))   # Sat 8 AM

@test("next_window_open: after Monday close → Tuesday 6 AM")
def _():
    after = pt(0, 14, 0)  # Mon 2 PM PT
    nxt = next_window_open(after)
    assert_eq(nxt.weekday(), 1)  # Tuesday
    assert_eq(nxt.hour, 6)
    assert_eq(nxt.minute, 0)

@test("next_window_open: Friday after close → Monday 6 AM (skip weekend)")
def _():
    fri_close = pt(4, 14, 0)
    nxt = next_window_open(fri_close)
    assert_eq(nxt.weekday(), 0)  # Monday

@test("next_window_open: Saturday → Monday 6 AM")
def _():
    sat = pt(5, 10, 0)
    nxt = next_window_open(sat)
    assert_eq(nxt.weekday(), 0)


# ===========================================================
# 5. PATROL INVALIDATION
# ===========================================================
print("\n[5/7] Patrol invalidation logic")

# A pullback CALL watch on FVG zone [100, 102]. Threshold = 100 * 0.995 = 99.5.
def call_pb_watch():
    created = (datetime.utcnow() - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {"ticker": "TEST", "type": "CALL", "path": "PULLBACK",
            "zone_low": 100.0, "zone_high": 102.0, "created_at": created}

def put_pb_watch():
    created = (datetime.utcnow() - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {"ticker": "TEST", "type": "PUT", "path": "PULLBACK",
            "zone_low": 100.0, "zone_high": 102.0, "created_at": created}

def call_brk_watch():
    created = (datetime.utcnow() - timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {"ticker": "TEST", "type": "CALL", "path": "BREAKOUT",
            "zone_low": 99.7, "zone_high": 100.3, "created_at": created}  # level=100.0

@test("CALL pullback invalidated if close < FVG_low * 0.995")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=8)
    df = make_bars([
        (101, 101.5, 100.5, 101.0, 1000),  # holding
        (100.5, 100.5, 99.0, 99.2, 1000),  # close 99.2 < 99.5 threshold → invalidated
    ], base_ts=base)
    ev = check_invalidation(call_pb_watch(), df)
    assert ev is not None, "should flag invalidation"
    assert_eq(ev['type'], 'PATROL_INVALIDATION')
    assert_eq(ev['reason'], 'fvg_breach_down')

@test("CALL pullback NOT invalidated if close stays above threshold")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=8)
    df = make_bars([
        (101, 101.5, 100.0, 100.5, 1000),
        (100.5, 100.8, 99.6, 99.7, 1000),  # 99.7 > 99.5 threshold
    ], base_ts=base)
    ev = check_invalidation(call_pb_watch(), df)
    assert ev is None, "should NOT flag — close above threshold"

@test("PUT pullback invalidated if close > FVG_high * 1.005")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=8)
    df = make_bars([
        (101, 101.5, 100.5, 101.0, 1000),
        (101, 103.0, 100.8, 102.8, 1000),  # close 102.8 > 102.51 threshold → invalidated
    ], base_ts=base)
    ev = check_invalidation(put_pb_watch(), df)
    assert ev is not None
    assert_eq(ev['reason'], 'fvg_breach_up')

@test("BREAKOUT CALL invalidated if close < level * 0.995")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=8)
    df = make_bars([
        (100.2, 100.5, 99.8, 100.1, 1000),
        (100.0, 100.0, 99.0, 99.3, 1000),  # close 99.3 < 99.5 threshold → failed breakout
    ], base_ts=base)
    ev = check_invalidation(call_brk_watch(), df)
    assert ev is not None
    assert_eq(ev['reason'], 'failed_breakout_pullback')

@test("Empty dataframe returns None (no false invalidation)")
def _():
    assert check_invalidation(call_pb_watch(), pd.DataFrame()) is None

@test("Bars before watch creation are ignored")
def _():
    # Bars way in the past — should be filtered out
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(hours=10)
    df = make_bars([
        (100, 100, 90, 92, 1000),  # huge breach but BEFORE watch was created
    ], base_ts=base)
    ev = check_invalidation(call_pb_watch(), df)
    assert ev is None


# ===========================================================
# 6. PATROL FAST RETEST
# ===========================================================
print("\n[6/7] Patrol fast retest (1-min wick logic)")

@test("CALL pullback fires on rejection wick into zone")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5)
    df = make_bars([
        (103, 103.5, 102.5, 103.0, 1000),
        # Bar enters zone, lower wick > 40% of range, close > open
        (101.5, 102.0, 100.5, 101.8, 1000),
        # range = 102.0-100.5 = 1.5; lower_wick = min(101.5,101.8)-100.5 = 1.0
        # 1.0/1.5 = 0.67 > 0.4, close 101.8 > open 101.5 → BULL REJECTION ✓
    ], base_ts=base)
    ev = check_fast_retest(call_pb_watch(), df)
    assert ev is not None
    assert_eq(ev['type'], 'PATROL_FAST_RETEST')
    assert_eq(ev['pattern'], 'bull_rejection_1min')

@test("CALL pullback ignores bars that don't enter the zone")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5)
    df = make_bars([
        (103, 104, 102.5, 103.5, 1000),
        (103.5, 103.8, 102.5, 103.5, 1000),  # never enters [100, 102]
    ], base_ts=base)
    ev = check_fast_retest(call_pb_watch(), df)
    assert ev is None

@test("CALL pullback does NOT fire on small wick (<40%)")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5)
    df = make_bars([
        (101.0, 102.0, 100.5, 101.8, 1000),
        # range=1.5, lower_wick=min(101,101.8)-100.5=0.5 → 0.5/1.5=0.33 < 0.4
    ], base_ts=base)
    ev = check_fast_retest(call_pb_watch(), df)
    assert ev is None, "lower wick too small to count as rejection"

@test("PUT pullback fires on bear rejection wick (upper wick + close < open)")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5)
    df = make_bars([
        # range = 103.5-100.5 = 3; upper_wick = 103.5 - max(100.8, 100.7) = 2.7 → 2.7/3 = 0.9 > 0.4
        # close 100.7 < open 100.8 → BEAR REJECTION ✓
        (100.8, 103.5, 100.5, 100.7, 1000),
    ], base_ts=base)
    ev = check_fast_retest(put_pb_watch(), df)
    assert ev is not None
    assert_eq(ev['pattern'], 'bear_rejection_1min')

@test("BREAKOUT CALL fires on close above level with strong body")
def _():
    base = datetime.utcnow().replace(tzinfo=UTC) - timedelta(minutes=5)
    df = make_bars([
        # level = 100.0; close 100.8 > level; body = (close-open)/range
        # range = 101.0 - 100.0 = 1.0; body = (100.8-100.1)/1.0 = 0.7 > 0.5 ✓
        (100.1, 101.0, 100.0, 100.8, 1000),
    ], base_ts=base)
    ev = check_fast_retest(call_brk_watch(), df)
    assert ev is not None
    assert_eq(ev['pattern'], 'breakout_confirm_1min')


# ===========================================================
# 7. ENGINE HEALTH CHECK
# ===========================================================
print("\n[7/7] Engine health check (signals.json freshness)")

@test("Fresh signals.json (mtime < threshold) returns None")
def _():
    # Touch the existing signals.json to make it fresh
    sigs = os.path.join(HERE, 'v4_signals.json')
    if os.path.exists(sigs):
        os.utime(sigs, None)  # touch
    ev = check_engine_health()
    assert ev is None, "fresh file should not be stale"

@test("Stale signals.json (mtime > 10min old) emits PATROL_ENGINE_STALE")
def _():
    sigs = os.path.join(HERE, 'v4_signals.json')
    if not os.path.exists(sigs):
        # Create one for the test
        with open(sigs, 'w') as f:
            json.dump({"metadata": {}, "signals": []}, f)
    # Backdate it 15 min
    fifteen_min_ago = datetime.now().timestamp() - 15 * 60
    os.utime(sigs, (fifteen_min_ago, fifteen_min_ago))
    ev = check_engine_health()
    assert ev is not None, "should flag stale"
    assert_eq(ev['type'], 'PATROL_ENGINE_STALE')
    assert ev['signals_age_seconds'] > STALE_ENGINE_THRESHOLD_SEC
    # Restore: touch fresh so we don't poison other tests / live system
    os.utime(sigs, None)


# ===========================================================
# 8. PAPER TRADER — pure logic (no network)
# ===========================================================
print("\n[8/10] Paper trader — pure logic (no network calls)")

from v4_paper_trader import (
    signal_id_for, has_open_for, open_count,
    MAX_CONCURRENT_POSITIONS, MAX_POSITION_USD, BASE_SIZE_USD,
)

@test("signal_id_for is deterministic across runs")
def _():
    sig = {'Ticker': 'AMZN', 'Type': 'CALL', 'Timestamp': '2026-04-17T14:30:00Z'}
    a = signal_id_for(sig)
    b = signal_id_for(sig)
    assert_eq(a, b, "should be stable")
    assert 'AMZN' in a and 'CALL' in a and '2026-04-17' in a

@test("signal_id differs by ticker, direction, date")
def _():
    base = {'Ticker': 'AMZN', 'Type': 'CALL', 'Timestamp': '2026-04-17T14:30:00Z'}
    other_ticker = {**base, 'Ticker': 'MSFT'}
    other_dir = {**base, 'Type': 'PUT'}
    other_date = {**base, 'Timestamp': '2026-04-18T14:30:00Z'}
    assert signal_id_for(base) != signal_id_for(other_ticker)
    assert signal_id_for(base) != signal_id_for(other_dir)
    assert signal_id_for(base) != signal_id_for(other_date)

@test("has_open_for matches OPEN positions")
def _():
    positions = [
        {'ticker': 'AMZN', 'direction': 'CALL', 'status': 'OPEN'},
        {'ticker': 'MSFT', 'direction': 'CALL', 'status': 'CLOSED'},
    ]
    assert has_open_for(positions, 'AMZN', 'CALL')
    assert not has_open_for(positions, 'AMZN', 'PUT')
    assert not has_open_for(positions, 'MSFT', 'CALL')  # closed
    assert not has_open_for(positions, 'GOOGL', 'CALL')  # absent

@test("has_open_for treats PENDING_ENTRY as open (no double-orders)")
def _():
    positions = [{'ticker': 'AMZN', 'direction': 'CALL', 'status': 'PENDING_ENTRY'}]
    assert has_open_for(positions, 'AMZN', 'CALL'), "PENDING should block new entry"

@test("open_count counts OPEN only")
def _():
    positions = [
        {'status': 'OPEN'}, {'status': 'OPEN'}, {'status': 'PENDING_ENTRY'},
        {'status': 'CLOSED'}, {'status': 'EXIT_FAILED'},
    ]
    assert_eq(open_count(positions), 2, "only OPEN counts toward concurrent cap")

@test("Risk caps configured to safe defaults")
def _():
    assert MAX_CONCURRENT_POSITIONS <= 10, "should cap concurrent positions"
    assert MAX_POSITION_USD <= 5000, "max per-position cap should be modest for paper"
    assert BASE_SIZE_USD <= MAX_POSITION_USD, "base size shouldn't exceed max"


# ===========================================================
# 9. PAPER JOURNAL — pure logic
# ===========================================================
print("\n[9/10] Paper journal — pure logic")

from v4_paper_journal import event_id, parse_ts, next_rth_open as journal_next_rth

@test("event_id is deterministic for identical inputs")
def _():
    a = event_id('ledger', 'TSLA', 'TRIGGER_PULLBACK_CONFIRMED', '2026-04-17T15:00:00Z')
    b = event_id('ledger', 'TSLA', 'TRIGGER_PULLBACK_CONFIRMED', '2026-04-17T15:00:00Z')
    assert_eq(a, b)
    assert len(a) == 16, "should be 16-char hash"

@test("event_id differs when any input changes")
def _():
    base = ('ledger', 'TSLA', 'TRIGGER_PULLBACK_CONFIRMED', '2026-04-17T15:00:00Z')
    a = event_id(*base)
    assert event_id('patrol', *base[1:]) != a
    assert event_id(base[0], 'MSFT', *base[2:]) != a
    assert event_id(*base[:2], 'WATCH_EXPIRED', base[3]) != a
    assert event_id(*base[:3], '2026-04-17T15:00:01Z') != a

@test("parse_ts handles standard UTC format")
def _():
    dt = parse_ts('2026-04-17T15:00:00Z')
    assert dt is not None
    assert dt.tzinfo is not None, "should be tz-aware"
    assert_eq(dt.year, 2026)
    assert_eq(dt.hour, 15)

@test("parse_ts returns None for garbage")
def _():
    assert parse_ts('') is None
    assert parse_ts(None) is None
    assert parse_ts('not a date') is None

@test("parse_ts handles fractional seconds gracefully")
def _():
    dt = parse_ts('2026-04-17T15:00:00.123456Z')
    assert dt is not None  # truncates to seconds, doesn't crash

@test("Journal's next_rth_open matches analyzer's behavior on after-hours")
def _():
    after_hours = datetime(2026, 4, 17, 1, 16, tzinfo=UTC)  # Fri 01:16Z = Thu 21:16 ET
    out = journal_next_rth(after_hours)
    assert out.weekday() == 4  # Friday
    assert_eq(out.hour, 13)
    assert_eq(out.minute, 30)


# ===========================================================
# 10. ATOMIC WRITE
# ===========================================================
print("\n[10/10] Atomic JSON write")

from v4_patrol_engine import atomic_write_json

@test("atomic_write_json creates a clean file")
def _():
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        atomic_write_json(tmp, {"x": 1, "y": [1, 2, 3]})
        with open(tmp) as f:
            data = json.load(f)
        assert_eq(data, {"x": 1, "y": [1, 2, 3]})
    finally:
        os.unlink(tmp)

@test("atomic_write_json overwrites existing file safely")
def _():
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        atomic_write_json(tmp, {"first": True})
        atomic_write_json(tmp, {"second": True})
        with open(tmp) as f:
            data = json.load(f)
        assert_eq(data, {"second": True})
    finally:
        os.unlink(tmp)

@test("atomic_write doesn't leave .tmp_* files on success")
def _():
    import tempfile, glob
    test_dir = tempfile.mkdtemp()
    try:
        target = os.path.join(test_dir, 'out.json')
        atomic_write_json(target, {"ok": True})
        leftover = glob.glob(os.path.join(test_dir, '.tmp_*'))
        assert_eq(len(leftover), 0, "no temp files should remain")
        assert os.path.exists(target)
    finally:
        import shutil
        shutil.rmtree(test_dir)


# ===========================================================
# SUMMARY
# ===========================================================
print()
print("=" * 60)
print(f"RESULTS: {T.passed} passed, {T.failed} failed")
print("=" * 60)
if T.failed > 0:
    print("\nFailures:")
    for name, err in T.failures:
        print(f"  ✗ {name}\n    {err}")
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
