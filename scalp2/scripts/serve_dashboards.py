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
    """Index.html redirects to /trade — manual entry is the default landing."""
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=trade.html">
  <title>scalp 2</title>
</head>
<body>
  <p>Loading… <a href="trade.html">trade</a> · <a href="auto.html">auto</a></p>
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
from urllib.parse import parse_qs, urlparse


def _send_json(handler, payload: dict, status: int = 200) -> None:
    body = _json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_post_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return _json.loads(raw)
    except Exception:
        return {}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            code = int(args[1]) if len(args) >= 2 else 0
        except (ValueError, IndexError):
            code = 0
        if code and code >= 400:
            _log(f"HTTP {args[0]} → {args[1]}")

    # ─── GET ──────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # /api/live — full account/positions/orders snapshot for trade page
        if path == "/api/live":
            with _LIVE_LOCK:
                return _send_json(self, _LIVE_STATE)

        # /api/quote?symbol=XYZ — current bid/ask/last/spread for one ticker
        if path == "/api/quote":
            qs = parse_qs(parsed.query)
            sym = (qs.get("symbol") or [""])[0].upper().strip()
            if not sym:
                return _send_json(self, {"error": "symbol required"}, 400)
            try:
                from data_clients.alpaca import AlpacaClient
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                with AlpacaClient() as a:
                    end = _dt.now(tz=_tz.utc)
                    start = end - _td(minutes=5)
                    quotes = a.get_quotes(sym,
                                            start=start.isoformat(timespec="seconds"),
                                            end=end.isoformat(timespec="seconds"),
                                            limit=1)
                    bars = a.get_bars(sym,
                                        start=start.isoformat(timespec="seconds"),
                                        end=end.isoformat(timespec="seconds"),
                                        timeframe="1Min", limit=5)
                if not quotes and not bars:
                    return _send_json(self, {"symbol": sym, "error": "no data"}, 404)
                q = quotes[-1] if quotes else None
                last = bars[-1].c if bars else (q.midpoint if q else None)
                return _send_json(self, {
                    "symbol": sym,
                    "bid": q.bid if q else None,
                    "ask": q.ask if q else None,
                    "spread_pct": q.spread_pct if q else None,
                    "last": last,
                    "ts": end.isoformat(timespec="seconds") + "Z",
                })
            except Exception as e:
                return _send_json(self, {"symbol": sym, "error": str(e)}, 500)

        # /api/daemon/status — full paper trader status
        if path == "/api/daemon/status":
            from scripts.paper_trader import TRADER
            return _send_json(self, TRADER.status.to_dict())

        # /api/diag — connection probes + DB stats + daemon health
        if path == "/api/diag":
            from scripts.paper_trader import TRADER, DB_PATH
            import sqlite3
            probes: dict = {}
            # Alpaca probe
            try:
                from data_clients.alpaca import AlpacaClient
                with AlpacaClient() as a:
                    acct = a.get_account()
                    probes["alpaca"] = {
                        "ok": True, "is_live": a.is_live,
                        "equity": acct.equity, "bp": acct.buying_power,
                        "daytrade": acct.daytrade_count,
                    }
            except Exception as e:
                probes["alpaca"] = {"ok": False, "error": str(e)}
            # UW probe
            try:
                from data_clients.unusual_whales import UWClient
                uw = UWClient()
                # Light test — fetch flow_recent for SPY (typically returns quickly)
                recs = uw.flow_recent("SPY")
                probes["uw"] = {"ok": True, "spy_flow_records": len(recs)}
            except Exception as e:
                probes["uw"] = {"ok": False, "error": str(e)}
            # SQLite probe
            try:
                con = sqlite3.connect(str(DB_PATH))
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                setups_n = con.execute("SELECT COUNT(*) FROM setups").fetchone()[0]
                decisions_n = con.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
                con.close()
                probes["sqlite"] = {
                    "ok": True, "tables": tables,
                    "setups_rows": setups_n,
                    "decisions_rows": decisions_n,
                }
            except Exception as e:
                probes["sqlite"] = {"ok": False, "error": str(e)}
            # Daemon health
            s = TRADER.status.to_dict()
            probes["daemon"] = {
                "running": s["running"], "phase": s.get("phase"),
                "tickers": len(s["tickers"]),
                "setups": len(s.get("setups") or []),
                "auto_submit": s["auto_submit"],
                "last_tick_at": s.get("last_tick_at"),
            }
            return _send_json(self, probes)

        # /api/prebreakout?ticker=X&side=CALL — composite pre-breakout score
        # 501 stub. Per design doc, M1-M7 (~33h) of build before this is real.
        if path == "/api/prebreakout":
            return _send_json(self, {
                "error": "not implemented",
                "status": "ui_scaffold_only",
                "next_build": "M1 Polygon aux client (4h) → M2 F1-F4 (8h) → "
                              "M3 UW wrapper (4h) → M4 F5-F6 (6h) → "
                              "M5 composite scorer (3h) → M7 audit page (4h)",
            }, 501)

        # /api/review?date=YYYY-MM-DD — daily EOD review (cached unless force=1)
        if path == "/api/review":
            from journal.eod_analyzer import analyze_day
            qs = parse_qs(parsed.query)
            date_str = (qs.get("date") or [""])[0] or None
            force = (qs.get("force") or ["0"])[0] == "1"
            try:
                review = analyze_day(date_str, force=force)
                return _send_json(self, review)
            except Exception as e:
                return _send_json(self, {"error": f"{type(e).__name__}: {e}"}, 500)

        # /api/setups — current per-ticker setup cards (FORMING + TRADE)
        # This is what /trade.html polls every 3s.
        if path == "/api/setups":
            from scripts.paper_trader import TRADER
            s = TRADER.status.to_dict()
            return _send_json(self, {
                "running": s["running"],
                "auto_submit": s["auto_submit"],
                "market_open": s["market_open"],
                "last_tick_at": s["last_tick_at"],
                "setups": s["setups"],
            })

        # Fallback: serve static files
        return super().do_GET()

    # ─── POST ─────────────────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # /api/order — submit equity bracket on paper account
        if path == "/api/order":
            body = _read_post_body(self)
            sym = (body.get("symbol") or "").upper().strip()
            side = (body.get("side") or "").lower().strip()
            qty = int(body.get("qty") or 0)
            target = float(body.get("take_profit") or 0)
            stop = float(body.get("stop_loss") or 0)
            if not sym or side not in ("buy", "sell") or qty <= 0:
                return _send_json(self, {"error": "need symbol, side=buy|sell, qty>0"}, 400)
            if target <= 0 or stop <= 0:
                return _send_json(self, {"error": "need take_profit + stop_loss > 0"}, 400)
            try:
                from data_clients.alpaca import AlpacaClient
                with AlpacaClient() as a:
                    resp = a.submit_equity_bracket(
                        symbol=sym, qty=qty, side=side,
                        take_profit_price=target, stop_loss_price=stop,
                        client_order_id=f"edge-manual-{sym}-{int(time.time())}",
                    )
                _log(f"manual order: {sym} {side} {qty} → {resp.get('id')}")
                return _send_json(self, resp)
            except Exception as e:
                return _send_json(self, {"error": f"{type(e).__name__}: {e}"}, 500)

        # /api/close — close a single position (POST {symbol})
        if path == "/api/close":
            body = _read_post_body(self)
            sym = (body.get("symbol") or "").upper().strip()
            if not sym:
                return _send_json(self, {"error": "symbol required"}, 400)
            try:
                from data_clients.alpaca import AlpacaClient
                with AlpacaClient() as a:
                    r = a._trading.delete(f"/v2/positions/{sym}",
                                            params={"percentage": "100"})
                    r.raise_for_status()
                    return _send_json(self, r.json())
            except Exception as e:
                return _send_json(self, {"error": f"{type(e).__name__}: {e}"}, 500)

        # /api/close_all — kill switch
        if path == "/api/close_all":
            try:
                from data_clients.alpaca import AlpacaClient
                with AlpacaClient() as a:
                    return _send_json(self, {"closed": a.close_all_positions()})
            except Exception as e:
                return _send_json(self, {"error": f"{type(e).__name__}: {e}"}, 500)

        # /api/daemon/start — start the paper trader (auto_submit optional)
        if path == "/api/daemon/start":
            from scripts.paper_trader import TRADER, DEFAULT_UNIVERSE
            body = _read_post_body(self)
            tickers = body.get("tickers") or list(DEFAULT_UNIVERSE)
            poll = int(body.get("poll_seconds") or 60)
            auto = bool(body.get("auto_submit") or False)
            if isinstance(tickers, str):
                tickers = [t.strip().upper() for t in tickers.split(",") if t.strip()]
            r = TRADER.start(tickers=tickers, poll_seconds=poll, auto_submit=auto)
            _log(f"daemon start: {r}")
            return _send_json(self, {**r, "status": TRADER.status.to_dict()})

        # /api/daemon/stop
        if path == "/api/daemon/stop":
            from scripts.paper_trader import TRADER
            r = TRADER.stop()
            _log(f"daemon stop: {r}")
            return _send_json(self, {**r, "status": TRADER.status.to_dict()})

        # /api/daemon/auto — toggle auto_submit without restarting the loop
        if path == "/api/daemon/auto":
            from scripts.paper_trader import TRADER
            body = _read_post_body(self)
            r = TRADER.set_auto_submit(bool(body.get("auto_submit", False)))
            _log(f"daemon auto_submit: {r}")
            return _send_json(self, r)

        # /api/test_tick — force-evaluate one ticker NOW (bypass phase gate).
        # Use this to verify the patrol wiring end-to-end before market open.
        if path == "/api/test_tick":
            from scripts.paper_trader import TRADER
            body = _read_post_body(self)
            tk = (body.get("ticker") or "").strip().upper()
            if not tk:
                return _send_json(self, {"error": "ticker required"}, 400)
            r = TRADER.force_test_tick(tk)
            _log(f"test_tick {tk}: state={r.get('state')} setups={r.get('setups_count')}")
            return _send_json(self, r)

        # /api/take — manual: submit a TRADE setup card via id
        if path == "/api/take":
            from scripts.paper_trader import TRADER
            body = _read_post_body(self)
            sid = (body.get("id") or "").strip()
            if not sid:
                return _send_json(self, {"error": "id required"}, 400)
            r = TRADER.take_setup(sid)
            _log(f"manual take: {sid} → {r.get('ok')}")
            return _send_json(self, r, 200 if r.get("ok") else 400)

        return _send_json(self, {"error": "not found"}, 404)


class _ReusableTCPServer(socketserver.ThreadingTCPServer):
    # Set BEFORE bind so TIME_WAIT sockets don't block restart.
    allow_reuse_address = True
    daemon_threads = True


def _serve_forever(port: int) -> None:
    os.chdir(OUTPUT_DIR)
    with _ReusableTCPServer(("", port), _QuietHandler) as httpd:
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

    # Load secrets ONCE at server boot so all background threads see env vars.
    try:
        from infra.secrets import load_secrets
        load_secrets()
        _log("secrets loaded")
    except Exception as e:
        _log(f"WARNING: load_secrets failed: {e}")

    pid_file = DATA_DIR / "dashboard_server.pid"
    pid_file.write_text(str(os.getpid()))

    # Initial setup: index + live state (pages themselves are static HTML)
    _write_index(args.port)
    _refresh_live_state()

    # Background live-state loop (drives /api/live for browser polling)
    t2 = threading.Thread(target=_live_loop, args=(args.live_interval,), daemon=True)
    t2.start()

    # Auto-start the paper trader in scan-only mode so /trade.html shows
    # FORMING/TRADE cards without the user having to click "Start" first.
    # /auto.html flips auto_submit=True to enable autonomous execution.
    try:
        from scripts.paper_trader import TRADER, DEFAULT_UNIVERSE
        TRADER.start(tickers=list(DEFAULT_UNIVERSE), poll_seconds=60,
                      auto_submit=False)
        _log(f"paper trader auto-started (scan-only) · {len(DEFAULT_UNIVERSE)} tickers · poll 60s")
    except Exception as e:
        _log(f"paper trader auto-start FAILED: {e}")

    _log(f"started: live state refresh every {args.live_interval}s")
    _log(f"pages: /trade.html (signals)  ·  /auto.html (autonomous)")

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
