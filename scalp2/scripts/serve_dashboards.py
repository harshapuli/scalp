"""scripts/serve_dashboards.py — serve the live-account dashboard.

  $ python3 scripts/serve_dashboards.py [--port 8766] [--interval 300]

What it does:
  1. Renders trading_dashboard.html (live account / live trades / risk gates).
  2. Starts http.server on the chosen port serving scalp2/output/.
  3. Re-renders every `interval` seconds so the page stays fresh.

Hit Ctrl-C (or send SIGTERM) to stop. PID file at data/dashboard_server.pid.
Logs to data/dashboard_server.log so you can `tail -f` it.
"""
from __future__ import annotations

import argparse
import http.server
import os
import signal
import socketserver
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"
sys.path.insert(0, str(PROJECT_ROOT))


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)


def _write_index(port: int) -> None:
    """Index.html redirects to the trading dashboard — single page now."""
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=trading_dashboard.html">
  <title>scalp 2</title>
</head>
<body>
  <p>Loading dashboard… <a href="trading_dashboard.html">click here</a> if not redirected.</p>
</body>
</html>"""
    (OUTPUT_DIR / "index.html").write_text(html)


def _render_once() -> None:
    """Re-render the trading dashboard. Stdout suppressed except on error."""
    from contextlib import redirect_stdout
    import io

    _log("rendering trading_dashboard.html ...")
    try:
        from scripts import render_trading_dashboard
        buf = io.StringIO()
        with redirect_stdout(buf):
            render_trading_dashboard.main()
        for ln in buf.getvalue().splitlines():
            if ln.startswith("[render_trading] context summary:"):
                idx = buf.getvalue().splitlines().index(ln)
                for s in buf.getvalue().splitlines()[idx:idx + 6]:
                    _log("  " + s)
                break
    except Exception as e:
        _log(f"  trading render FAILED: {e}")
        traceback.print_exc()


# ─── Live JSON state for client-side polling ────────────────────────────────
# Cached by `_refresh_live_state()` every few seconds; the HTTP handler serves
# the cached blob from /api/live so each browser poll is cheap.

_LIVE_STATE: dict = {"generated_utc": "", "stale": True}
_LIVE_LOCK = threading.Lock()


def _refresh_live_state() -> None:
    """Pulls Alpaca account / positions / orders + dev_journal decisions and
    caches a JSON blob for /api/live. Cheap (one Alpaca call per category).
    """
    try:
        import sqlite3
        from data_clients.alpaca import AlpacaClient
        out: dict = {}
        with AlpacaClient() as a:
            acct = a.get_account()
            positions = a.get_positions()
            after = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).isoformat(timespec="seconds")
            try:
                orders = a.get_orders(status="all", after=after, limit=200)
            except Exception:
                orders = []
            out["account"] = {
                "equity": acct.equity, "buying_power": acct.buying_power,
                "daytrade_count": acct.daytrade_count, "is_live": a.is_live,
            }
            out["positions"] = [
                {"symbol": p.symbol, "qty": p.qty,
                 "avg_entry_price": p.avg_entry_price,
                 "current_price": p.current_price,
                 "market_value": p.market_value,
                 "unrealized_pl": p.unrealized_pl,
                 "unrealized_plpc": p.unrealized_plpc}
                for p in positions
            ]
            today_str = datetime.now(tz=timezone.utc).date().isoformat()
            out["live_orders"] = [
                o for o in orders
                if (o.get("submitted_at") or o.get("filled_at") or "")[:10] == today_str
            ][:50]

        # Decisions
        db_path = PROJECT_ROOT / "data" / "dev_journal.db"
        decisions: list[dict] = []
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT timestamp, strategy, ticker, decision, pass_reason, "
                        "ml_prob, expected_value, direction "
                        "FROM decision_log "
                        "WHERE date(timestamp) = date('now') "
                        "ORDER BY timestamp DESC LIMIT 100"
                    )
                    decisions = [
                        {"timestamp": r[0], "strategy": r[1], "ticker": r[2],
                         "decision": r[3], "pass_reason": r[4],
                         "ml_prob": r[5], "expected_value": r[6],
                         "direction": r[7]}
                        for r in cur.fetchall()
                    ]
            except Exception:
                pass
        out["decisions"] = decisions
        out["generated_utc"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
        out["stale"] = False

        with _LIVE_LOCK:
            _LIVE_STATE.clear()
            _LIVE_STATE.update(out)
    except Exception as e:
        _log(f"live-state refresh failed: {e}")
        with _LIVE_LOCK:
            _LIVE_STATE["stale"] = True
            _LIVE_STATE["error"] = str(e)


def _live_loop(interval_s: int) -> None:
    """Refresh the live JSON state every interval_s. This runs much faster
    than the full HTML render — typically every 3s for scalping cadence."""
    while not _stop_event.is_set():
        try:
            _refresh_live_state()
        except Exception as e:
            _log(f"live loop iteration failed: {e}")
        for _ in range(interval_s):
            if _stop_event.is_set():
                return
            time.sleep(1)


# Render-loop thread
_stop_event = threading.Event()


def _render_loop(interval_s: int) -> None:
    while not _stop_event.is_set():
        try:
            _render_once()
        except Exception as e:
            _log(f"render loop iteration failed: {e}")
        # Wait `interval_s` but be interruptible
        for _ in range(interval_s):
            if _stop_event.is_set():
                return
            time.sleep(1)


# HTTP server thread
import json as _json


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Minimal: only log non-200 responses to keep the server log clean.
        try:
            code = int(args[1]) if len(args) >= 2 else 0
        except (ValueError, IndexError):
            code = 0
        if code and code >= 400:
            _log(f"HTTP {args[0]} → {args[1]}")

    def do_GET(self):
        # /api/live — JSON snapshot of account/positions/orders/decisions
        if self.path.startswith("/api/live"):
            with _LIVE_LOCK:
                payload = _json.dumps(_LIVE_STATE, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # Fallback to file serving
        return super().do_GET()


def _serve_forever(port: int) -> None:
    os.chdir(OUTPUT_DIR)
    with socketserver.TCPServer(("", port), _QuietHandler) as httpd:
        httpd.allow_reuse_address = True
        _log(f"http.server listening on http://localhost:{port}/")
        try:
            httpd.serve_forever()
        except Exception as e:
            _log(f"http.server crashed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766,
                    help="Port to serve on (default 8766; 8765 is parallel project)")
    ap.add_argument("--interval", type=int, default=300,
                    help="HTML re-render interval in seconds (default 300 = 5 min)")
    ap.add_argument("--live-interval", type=int, default=3,
                    help="Live JSON refresh cadence in seconds (default 3s for scalping)")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pid_file = DATA_DIR / "dashboard_server.pid"
    pid_file.write_text(str(os.getpid()))

    # Initial render + index + live state
    _render_once()
    _write_index(args.port)
    _refresh_live_state()

    # Background HTML render loop (slow — for shell/structure changes)
    t1 = threading.Thread(target=_render_loop, args=(args.interval,), daemon=True)
    t1.start()
    # Background live-state loop (fast — drives /api/live for browser polling)
    t2 = threading.Thread(target=_live_loop, args=(args.live_interval,), daemon=True)
    t2.start()
    _log(f"started: HTML render every {args.interval}s, live state every {args.live_interval}s")

    # Foreground HTTP server
    def _shutdown(signum, frame):
        _log(f"received signal {signum}, shutting down...")
        _stop_event.set()
        try:
            pid_file.unlink()
        except Exception:
            pass
        os._exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _serve_forever(args.port)
    finally:
        _stop_event.set()
        try:
            pid_file.unlink()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
