"""
V4 PATROL ENGINE — fast-loop monitor for live conditions the 5-min engine misses.

DESIGN PRINCIPLE: read-only. The patrol observes and reports. It does NOT
modify v4_watches.json, v4_signals.json, or v4_ledger.json. Decisions to
kill watches or fire trades stay with the main engine (or with you).
This keeps the patrol safe to run in parallel without race conditions.

WHAT IT WATCHES (per cycle):
  1. STRUCTURE INVALIDATION — for each active watch, check if recent 1-min
     bars have closed beyond the structural invalidation threshold:
       - Pullback CALL: close < FVG_low * 0.995 (FVG breached)
       - Pullback PUT:  close > FVG_high * 1.005
       - Breakout CALL: close < breakout_level * 0.995 (failed breakout)
       - Breakout PUT:  close > breakdown_level * 1.005
     → emits PATROL_INVALIDATION

  2. FAST SMC RETEST — same retest logic as main engine but on 1-min bars.
     Catches retests that bounce within seconds and would be missed by the
     5/15/60-min bars the engine uses.
     → emits PATROL_FAST_RETEST

  3. ENGINE HEALTH — checks signals.json mtime. If stale > 10 min during
     market hours, surfaces a heartbeat warning.
     → emits PATROL_ENGINE_STALE

CADENCE: 30 sec default (configurable). Honors same market window as Armada
(6:00 AM – 1:30 PM PT, Mon–Fri).

API BUDGET: ~1 Alpaca call per active watch per cycle. Zero UW calls.
At 5 watches × 30-sec cycle × 7.5h = 4,500 Alpaca calls/day. Free.

OUTPUT:
  - stdout: human-readable lines for tailing
  - v4_patrol_log.jsonl: append-only JSON Lines for later analysis
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHES_PATH = os.path.join(BASE_DIR, 'v4_watches.json')
SIGNALS_PATH = os.path.join(BASE_DIR, 'v4_signals.json')
PREPOSITION_PATH = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')
PATROL_LOG_PATH = os.path.join(BASE_DIR, 'v4_patrol_log.jsonl')
KILL_FILE = os.path.join(BASE_DIR, 'v4_patrol_kill')  # touch this to stop the patrol

# Pre-position trigger thresholds
APPROACHING_PCT = 0.005   # within 0.5% of trigger
CONFIRMED_BARS = 5        # 5 bars (5 min on 1-min) above trigger = confirmed
FAILED_BUFFER = 0.003     # close back 0.3% below trigger = failed breakout

# Cadence
PATROL_CADENCE_SEC = 30
PATROL_OUTSIDE_CADENCE_SEC = 300  # slow poll outside window
STALE_ENGINE_THRESHOLD_SEC = 600  # 10 min since main engine wrote signals.json

# Invalidation thresholds (% beyond the structural level required to count as broken)
INVALIDATION_BUFFER = 0.005  # 0.5%

# Market window — must mirror v4_armada.py
WINDOW_START = (6, 0)
WINDOW_END = (13, 30)


def log(msg: str, level: str = "INFO"):
    ts = datetime.now(UTC).strftime('%H:%M:%SZ')
    icon = {"INFO": "🛡️", "WARN": "⚠️", "ALERT": "🚨", "OK": "✓"}.get(level, "·")
    print(f"[{ts}] {icon} PATROL: {msg}", flush=True)


def emit_event(event: dict):
    """Append a structured event to the JSONL log. stdout already has a human line."""
    event['ts_utc'] = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with open(PATROL_LOG_PATH, 'a') as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        log(f"failed to write event log: {e}", "WARN")


def atomic_write_json(path: str, data: dict):
    """Write JSON atomically (write → rename) so concurrent readers never see partial files."""
    import tempfile
    dir_path = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass
        raise


def in_window(now_pt: datetime) -> bool:
    if now_pt.weekday() >= 5: return False
    cur = now_pt.hour * 60 + now_pt.minute
    return WINDOW_START[0]*60 + WINDOW_START[1] <= cur <= WINDOW_END[0]*60 + WINDOW_END[1]


def load_watches() -> list:
    if not os.path.exists(WATCHES_PATH): return []
    try:
        with open(WATCHES_PATH) as f: return json.load(f)
    except Exception:
        return []


def fetch_recent_1min(ticker: str, minutes_back: int = 60):
    """Pull the last N minutes of 1-min bars."""
    end = datetime.utcnow()
    start = end - timedelta(minutes=minutes_back + 5)  # small cushion
    df = fetch_alpaca_bars(
        ticker, '1Min',
        start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        end.strftime('%Y-%m-%dT%H:%M:%SZ'),
    )
    return df


def check_invalidation(watch: dict, df_1min) -> dict:
    """Return event dict if structure invalidation has occurred since watch creation, else None."""
    if df_1min is None or df_1min.empty: return None
    try:
        created = datetime.strptime(watch['created_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
    except Exception:
        return None
    bars = df_1min[df_1min.index >= created]
    if bars.empty: return None

    path = watch.get('path')
    ttype = watch['type']
    zone_low = watch.get('zone_low')
    zone_high = watch.get('zone_high')
    if zone_low is None or zone_high is None: return None

    closes = bars['Close'].astype(float)

    if path == 'PULLBACK':
        # FVG breached = price closed beyond the zone in the wrong direction
        if ttype == 'CALL':
            threshold = zone_low * (1 - INVALIDATION_BUFFER)
            breached = closes[closes < threshold]
            if not breached.empty:
                worst = breached.min()
                return {
                    "type": "PATROL_INVALIDATION", "ticker": watch['ticker'],
                    "watch_type": ttype, "watch_path": path,
                    "reason": "fvg_breach_down",
                    "threshold": round(threshold, 2),
                    "worst_close": round(float(worst), 2),
                    "fvg_low": round(zone_low, 2), "fvg_high": round(zone_high, 2),
                }
        else:  # PUT
            threshold = zone_high * (1 + INVALIDATION_BUFFER)
            breached = closes[closes > threshold]
            if not breached.empty:
                worst = breached.max()
                return {
                    "type": "PATROL_INVALIDATION", "ticker": watch['ticker'],
                    "watch_type": ttype, "watch_path": path,
                    "reason": "fvg_breach_up",
                    "threshold": round(threshold, 2),
                    "worst_close": round(float(worst), 2),
                    "fvg_low": round(zone_low, 2), "fvg_high": round(zone_high, 2),
                }
    else:  # BREAKOUT
        level = (zone_low + zone_high) / 2
        if ttype == 'CALL':
            # Failed breakout = closed back below the level by >0.5%
            threshold = level * (1 - INVALIDATION_BUFFER)
            breached = closes[closes < threshold]
            if not breached.empty:
                worst = breached.min()
                return {
                    "type": "PATROL_INVALIDATION", "ticker": watch['ticker'],
                    "watch_type": ttype, "watch_path": path,
                    "reason": "failed_breakout_pullback",
                    "level": round(level, 2),
                    "threshold": round(threshold, 2),
                    "worst_close": round(float(worst), 2),
                }
        else:  # PUT
            threshold = level * (1 + INVALIDATION_BUFFER)
            breached = closes[closes > threshold]
            if not breached.empty:
                worst = breached.max()
                return {
                    "type": "PATROL_INVALIDATION", "ticker": watch['ticker'],
                    "watch_type": ttype, "watch_path": path,
                    "reason": "failed_breakdown_recovery",
                    "level": round(level, 2),
                    "threshold": round(threshold, 2),
                    "worst_close": round(float(worst), 2),
                }
    return None


def check_fast_retest(watch: dict, df_1min) -> dict:
    """Check 1-min bars for SMC retest pattern. Same logic as engine but tighter timeframe."""
    if df_1min is None or df_1min.empty: return None
    try:
        created = datetime.strptime(watch['created_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
    except Exception:
        return None
    bars = df_1min[df_1min.index >= created]
    if bars.empty: return None

    path = watch.get('path')
    ttype = watch['type']
    zone_low = watch.get('zone_low')
    zone_high = watch.get('zone_high')
    if zone_low is None or zone_high is None: return None

    for ts, bar in bars.iterrows():
        high = float(bar['High']); low = float(bar['Low'])
        o = float(bar['Open']); c = float(bar['Close'])
        rng = high - low
        if rng <= 0: continue

        if path == 'PULLBACK':
            entered = low <= zone_high and high >= zone_low
            if not entered: continue
            if ttype == 'CALL':
                lower_wick = min(o, c) - low
                if lower_wick / rng > 0.4 and c > o:
                    return {
                        "type": "PATROL_FAST_RETEST", "ticker": watch['ticker'],
                        "watch_type": ttype, "watch_path": path,
                        "bar_ts": ts.strftime('%H:%M'),
                        "trigger_price": round(c, 2),
                        "pattern": "bull_rejection_1min",
                        "fvg_low": round(zone_low, 2), "fvg_high": round(zone_high, 2),
                    }
            else:
                upper_wick = high - max(o, c)
                if upper_wick / rng > 0.4 and c < o:
                    return {
                        "type": "PATROL_FAST_RETEST", "ticker": watch['ticker'],
                        "watch_type": ttype, "watch_path": path,
                        "bar_ts": ts.strftime('%H:%M'),
                        "trigger_price": round(c, 2),
                        "pattern": "bear_rejection_1min",
                        "fvg_low": round(zone_low, 2), "fvg_high": round(zone_high, 2),
                    }
        else:  # BREAKOUT
            level = (zone_low + zone_high) / 2
            if ttype == 'CALL':
                if c > level and (c - o) / rng > 0.5:
                    return {
                        "type": "PATROL_FAST_RETEST", "ticker": watch['ticker'],
                        "watch_type": ttype, "watch_path": path,
                        "bar_ts": ts.strftime('%H:%M'),
                        "trigger_price": round(c, 2),
                        "pattern": "breakout_confirm_1min",
                        "level": round(level, 2),
                    }
            else:
                if c < level and (o - c) / rng > 0.5:
                    return {
                        "type": "PATROL_FAST_RETEST", "ticker": watch['ticker'],
                        "watch_type": ttype, "watch_path": path,
                        "bar_ts": ts.strftime('%H:%M'),
                        "trigger_price": round(c, 2),
                        "pattern": "breakdown_confirm_1min",
                        "level": round(level, 2),
                    }
    return None


def check_preposition_triggers():
    """Read pre-position candidates, check each against current price, update trigger_status in place.
    State machine: WAITING → APPROACHING → TRIGGERED → CONFIRMED (or FAILED if reverses)."""
    if not os.path.exists(PREPOSITION_PATH):
        return
    try:
        with open(PREPOSITION_PATH) as f:
            data = json.load(f)
    except Exception as e:
        log(f"prep file read failed: {e}", "WARN")
        return

    candidates = data.get('candidates') or []
    if not candidates:
        return

    log(f"checking {len(candidates)} pre-position candidate trigger(s)...")
    now_utc_str = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    changed = False

    for c in candidates:
        ticker = c.get('ticker')
        direction = c.get('direction', 'CALL')
        lvls = c.get('key_levels') or {}
        trigger = lvls.get('trigger_above') if direction == 'CALL' else lvls.get('trigger_below') or lvls.get('compression_low')
        if not ticker or trigger is None:
            continue

        df = fetch_recent_1min(ticker, minutes_back=30)
        if df is None or df.empty:
            c['trigger_status'] = c.get('trigger_status') or 'WAITING'
            c['last_check_utc'] = now_utc_str
            continue

        spot = float(df['Close'].iloc[-1])
        c['current_spot'] = round(spot, 2)
        c['last_check_utc'] = now_utc_str

        # Distance to trigger as %
        if direction == 'CALL':
            distance_pct = (trigger - spot) / spot
            crossed = spot > trigger
        else:
            distance_pct = (spot - trigger) / spot
            crossed = spot < trigger
        c['distance_to_trigger_pct'] = round(distance_pct * 100, 3)

        old_status = c.get('trigger_status', 'WAITING')

        # Determine new status
        if crossed:
            # Count bars since cross — look at recent bars to see if it's just crossed or held
            if direction == 'CALL':
                bars_above = int((df['Close'] > trigger).tail(10).sum())
            else:
                bars_above = int((df['Close'] < trigger).tail(10).sum())
            c['bars_held'] = bars_above

            # Was previously triggered → check if reverted (failed breakout)
            failed_threshold = trigger * (1 - FAILED_BUFFER) if direction == 'CALL' else trigger * (1 + FAILED_BUFFER)
            recent_close = float(df['Close'].iloc[-1])
            reverted = (direction == 'CALL' and recent_close < failed_threshold) or \
                       (direction == 'PUT' and recent_close > failed_threshold)

            if old_status in ('TRIGGERED', 'CONFIRMED') and reverted:
                new_status = 'FAILED'
            elif bars_above >= CONFIRMED_BARS:
                new_status = 'CONFIRMED'
                # First time we see this candidate above trigger — record the cross.
                # (Without this, fast-path WAITING → CONFIRMED skips setting the field.)
                if not c.get('triggered_at_utc') and old_status not in ('TRIGGERED', 'CONFIRMED', 'FAILED'):
                    c['triggered_at_utc'] = now_utc_str
                    c['triggered_at_price'] = round(spot, 2)
            else:
                new_status = 'TRIGGERED'
                if not c.get('triggered_at_utc') and old_status not in ('TRIGGERED', 'CONFIRMED', 'FAILED'):
                    c['triggered_at_utc'] = now_utc_str
                    c['triggered_at_price'] = round(spot, 2)
        else:
            # Not crossed — APPROACHING vs WAITING
            if abs(distance_pct) <= APPROACHING_PCT:
                new_status = 'APPROACHING'
            else:
                new_status = 'WAITING'

        c['trigger_status'] = new_status

        # Log on transition
        if new_status != old_status:
            changed = True
            color_map = {'WAITING': '·', 'APPROACHING': '🟡', 'TRIGGERED': '🚨', 'CONFIRMED': '🟢', 'FAILED': '❌'}
            log(f"  {color_map.get(new_status,'·')} {ticker} {direction}: {old_status} → {new_status} "
                f"(spot={spot:.2f}, trigger={trigger:.2f}, dist={distance_pct*100:+.2f}%)",
                "ALERT" if new_status in ('TRIGGERED', 'CONFIRMED') else "INFO")
            emit_event({
                'type': 'PATROL_PREPOSITION_TRANSITION',
                'ticker': ticker, 'direction': direction,
                'from_status': old_status, 'to_status': new_status,
                'spot': spot, 'trigger': trigger,
                'distance_pct': round(distance_pct * 100, 3),
            })

    # Always write back — current_spot/last_check_utc updated even when status unchanged
    try:
        atomic_write_json(PREPOSITION_PATH, data)
    except Exception as e:
        log(f"failed to write prep file: {e}", "WARN")


def check_engine_health() -> dict:
    """Return event dict if main engine signals.json hasn't been updated in >10min."""
    if not os.path.exists(SIGNALS_PATH): return None
    try:
        mtime = os.path.getmtime(SIGNALS_PATH)
        age_sec = time.time() - mtime
        if age_sec > STALE_ENGINE_THRESHOLD_SEC:
            return {
                "type": "PATROL_ENGINE_STALE",
                "signals_age_seconds": int(age_sec),
                "signals_age_minutes": round(age_sec / 60, 1),
                "threshold_seconds": STALE_ENGINE_THRESHOLD_SEC,
            }
    except Exception:
        pass
    return None


def patrol_cycle():
    """Run one patrol pass over all active watches + pre-position triggers + engine health."""
    watches = load_watches()

    # Engine health first — independent of watches
    health = check_engine_health()
    if health:
        log(f"main engine signals.json is {health['signals_age_minutes']:.1f}min stale (>10min threshold)", "ALERT")
        emit_event(health)

    # Pre-position trigger monitoring (independent of v4 watches)
    check_preposition_triggers()

    if not watches:
        log(f"no active watches — patrol idle ({PATROL_CADENCE_SEC}s cycle)")
        return

    log(f"checking {len(watches)} active watch(es)...")

    for w in watches:
        ticker = w.get('ticker', '?')
        df = fetch_recent_1min(ticker, minutes_back=60)
        if df is None or df.empty:
            log(f"  {ticker}: no recent 1-min data (possibly halted or pre-market)", "WARN")
            continue

        inv = check_invalidation(w, df)
        if inv:
            log(f"  🚨 {ticker} {w['type']} {w['path']} INVALIDATED — {inv['reason']} "
                f"(close {inv['worst_close']} broke threshold {inv.get('threshold','?')})", "ALERT")
            emit_event(inv)
            continue  # skip retest check if invalidated

        retest = check_fast_retest(w, df)
        if retest:
            log(f"  🟢 {ticker} {w['type']} {w['path']} FAST RETEST @ {retest['bar_ts']} "
                f"price={retest['trigger_price']} ({retest['pattern']})", "ALERT")
            emit_event(retest)
            continue

        # Quiet pass
        last_close = float(df['Close'].iloc[-1])
        log(f"  · {ticker} {w['type']} {w['path']}: spot={last_close:.2f}, watching zone "
            f"[{w.get('zone_low','?'):.2f}, {w.get('zone_high','?'):.2f}]", "OK")


def main_loop():
    log("BOOTING V4 PATROL ENGINE...")
    log(f"Cadence: {PATROL_CADENCE_SEC}s in window, {PATROL_OUTSIDE_CADENCE_SEC}s outside")
    log(f"Watching window: {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}–"
        f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} PT (Mon–Fri)")
    log(f"Kill switch: touch {KILL_FILE} to stop")
    log(f"Event log: {PATROL_LOG_PATH}")

    while True:
        try:
            if os.path.exists(KILL_FILE):
                log("kill file detected — exiting cleanly", "WARN")
                os.remove(KILL_FILE)
                break

            now_pt = datetime.now(PACIFIC)
            if in_window(now_pt):
                try:
                    patrol_cycle()
                except Exception as e:
                    # Network blips (Alpaca timeouts, DNS hiccups) must NOT kill the loop.
                    log(f"cycle error caught — continuing: {type(e).__name__}: {str(e)[:200]}", "WARN")
                time.sleep(PATROL_CADENCE_SEC)
            else:
                log(f"outside window ({now_pt.strftime('%a %H:%M PT')}) — slow poll")
                time.sleep(PATROL_OUTSIDE_CADENCE_SEC)
        except KeyboardInterrupt:
            log("patrol interrupted by Ctrl-C", "WARN")
            break
        except Exception as e:
            # Outer safety net — log and retry rather than exit
            log(f"outer loop exception caught: {type(e).__name__}: {str(e)[:200]}", "ERR")
            time.sleep(30)


if __name__ == "__main__":
    main_loop()
