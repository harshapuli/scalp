import os
import sys
import time
import signal
import subprocess
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Path definition
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCANNER_PATH = os.path.abspath(os.path.join(BASE_DIR, '..', 'swing_trade_strategy', 'institutional_scanner.py'))
ENGINE_PATH = os.path.join(BASE_DIR, 'v4_meta_engine.py')
SERVER_PATH = os.path.join(BASE_DIR, 'v4_server.py')
ARTIFACT_ANALYZER_PATH = os.path.join(BASE_DIR, 'v4_ai_analyzer.py')
PATROL_PATH = os.path.join(BASE_DIR, 'v4_patrol_engine.py')

# Subprocess log paths (kept separate so daemon log stays clean)
SERVER_LOG = '/tmp/v4_server.log'
PATROL_LOG = '/tmp/v4_patrol.log'

# Scan window — daemon only runs the pipeline inside this window (Pacific Time).
# 6:00 AM PT covers premarket, 1:30 PM PT covers ~30 min after RTH close.
PACIFIC = ZoneInfo("America/Los_Angeles")
WINDOW_START = (6, 0)    # 6:00 AM PT
WINDOW_END = (13, 30)    # 1:30 PM PT
SLEEP_INSIDE_WINDOW = 300   # 5 min when active
SLEEP_OUTSIDE_WINDOW = 600  # 10 min when dormant (cheaper poll)


def log(msg):
    timestamp = datetime.utcnow().strftime('%H:%M:%SZ')
    print(f"[{timestamp}] ⚙️ SYSTEM DAEMON: {msg}", flush=True)


def in_scan_window(now_pt: datetime) -> bool:
    """True if `now_pt` falls inside the daily scan window (Mon-Fri only)."""
    if now_pt.weekday() >= 5:  # Sat=5, Sun=6
        return False
    cur_min = now_pt.hour * 60 + now_pt.minute
    start_min = WINDOW_START[0] * 60 + WINDOW_START[1]
    end_min = WINDOW_END[0] * 60 + WINDOW_END[1]
    return start_min <= cur_min <= end_min


def next_window_open(now_pt: datetime) -> datetime:
    """Return the next datetime when the scan window opens (in PT)."""
    candidate = now_pt.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    cur_min = now_pt.hour * 60 + now_pt.minute
    # If today's window has already closed (or it's the weekend), look at tomorrow
    if cur_min >= WINDOW_END[0] * 60 + WINDOW_END[1] or now_pt.weekday() >= 5:
        candidate += timedelta(days=1)
    # Skip weekends
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def run_pipeline():
    log("Spinning up active Screener...")
    subprocess.run([sys.executable, SCANNER_PATH])

    log("Piping Watchlist into V4 Execution Engine...")
    subprocess.run([sys.executable, ENGINE_PATH])

    log("Deploying AI Forensics & Shadow Book Audit...")
    subprocess.run([sys.executable, ARTIFACT_ANALYZER_PATH])

    log(f"Pipeline cycle complete. Next scan in {SLEEP_INSIDE_WINDOW}s.")


def write_idle_status(reason: str, next_open_pt: datetime):
    """Update v4_signals.json metadata so the dashboard shows the correct idle state."""
    import json
    sigs_path = os.path.join(BASE_DIR, 'v4_signals.json')
    try:
        if os.path.exists(sigs_path):
            with open(sigs_path) as f:
                data = json.load(f)
        else:
            data = {"signals": []}
        if 'metadata' not in data:
            data['metadata'] = {}
        data['metadata']['daemon_status'] = reason
        data['metadata']['next_window_open_pt'] = next_open_pt.strftime('%Y-%m-%dT%H:%M:%S%z')
        # Don't touch last_updated — dashboard uses that to know when engine last ran
        with open(sigs_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        log(f"Could not update idle status: {e}")


def boot_armada():
    log("BOOTING V4 ARMADA DAEMON...")
    log(f"Scan window: {WINDOW_START[0]:02d}:{WINDOW_START[1]:02d} – {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} PT (Mon–Fri)")

    log("Ensuring UI Backend is alive...")
    server_log = open(SERVER_LOG, 'a')
    server_proc = subprocess.Popen([sys.executable, SERVER_PATH], stdout=server_log, stderr=server_log)
    log(f"  Server PID {server_proc.pid} → {SERVER_LOG}")

    log("Spawning Patrol Engine (30s fast-loop monitor)...")
    patrol_log = open(PATROL_LOG, 'a')
    patrol_proc = subprocess.Popen([sys.executable, PATROL_PATH], stdout=patrol_log, stderr=patrol_log)
    log(f"  Patrol PID {patrol_proc.pid} → {PATROL_LOG}")

    # Clean shutdown on SIGTERM / SIGINT — kills both subprocesses, no orphans.
    def shutdown(signum, frame):
        log(f"Received signal {signum}. Killing subsystems (server PID {server_proc.pid}, patrol PID {patrol_proc.pid})...")
        for proc in (patrol_proc, server_proc):
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try: proc.kill()
                except Exception: pass
        log("All subsystems stopped. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            now_pt = datetime.now(PACIFIC)
            if in_scan_window(now_pt):
                log(f"In scan window (now: {now_pt.strftime('%H:%M PT')}). Running pipeline.")
                run_pipeline()
                time.sleep(SLEEP_INSIDE_WINDOW)
            else:
                noo = next_window_open(now_pt)
                wait_min = int((noo - now_pt).total_seconds() / 60)
                log(f"Outside window (now: {now_pt.strftime('%a %H:%M PT')}). "
                    f"Next open: {noo.strftime('%a %H:%M PT')} (~{wait_min} min). Sleeping {SLEEP_OUTSIDE_WINDOW}s.")
                write_idle_status("market_closed", noo)
                time.sleep(SLEEP_OUTSIDE_WINDOW)
    except KeyboardInterrupt:
        # Belt-and-suspenders — signal handler should catch first
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    boot_armada()
