"""V4 Dashboard Server — serves two parallel dashboards at /v1/ and /v2/.

  /v1/  → BASELINE strategy (Friday-close logic, no Phase 1-5 enhancements)
  /v2/  → ENHANCED strategy (current production)
  /     → redirects to /v2/ (default)

Each dashboard fetches its own API endpoints (suffixed accordingly) so the same
HTML/JS code base works for both modes. The dashboard JS auto-detects the URL
prefix and routes API calls to the right backing JSON file.
"""
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8084
BASE_DIR = os.path.dirname(__file__)
DIRECTORY = os.path.join(BASE_DIR, "dashboard")

# v2 (enhanced) — current production files
SIGNALS_FILE = os.path.join(BASE_DIR, "v4_signals.json")
WATCHES_FILE = os.path.join(BASE_DIR, "v4_watches.json")
LEDGER_FILE = os.path.join(BASE_DIR, "v4_ledger.json")
BREAKOUTS_FILE = os.path.join(BASE_DIR, "v4_preposition_watchlist.json")
POSITIONS_FILE = os.path.join(BASE_DIR, "v4_paper_positions.json")
GAPS_FILE = os.path.join(BASE_DIR, "v4_gap_watchlist.json")

# v1 (baseline) — shadow engine output
SIGNALS_BASELINE_FILE = os.path.join(BASE_DIR, "v4_signals_baseline.json")
WATCHES_BASELINE_FILE = os.path.join(BASE_DIR, "v4_watches_baseline.json")
LEDGER_BASELINE_FILE = os.path.join(BASE_DIR, "v4_ledger_baseline.json")

# Shared (no v1/v2 fork — same data)
MISSED_TRADES_FILE = os.path.join(BASE_DIR, "v4_missed_trades.json")


class V4DashboardServer(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def do_GET(self):
        path = self.path

        # Default → redirect to /v2/
        if path == '/' or path == '':
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
            return

        # ===== v2 ENHANCED API endpoints =====
        if path in ('/api/v4/signals', '/v2/api/v4/signals'):
            self._serve_json(SIGNALS_FILE, "v4_signals.json not found yet"); return
        if path in ('/api/v4/breakouts', '/v2/api/v4/breakouts'):
            self._serve_json(BREAKOUTS_FILE, "preposition watchlist not found yet"); return
        if path in ('/api/v4/positions', '/v2/api/v4/positions'):
            self._serve_positions(POSITIONS_FILE); return
        if path in ('/api/v4/gaps', '/v2/api/v4/gaps'):
            self._serve_json(GAPS_FILE, "v4_gap_watchlist.json not found yet"); return
        if path in ('/api/v4/missed', '/v2/api/v4/missed', '/v1/api/v4/missed'):
            self._serve_json(MISSED_TRADES_FILE,
                "v4_missed_trades.json not found yet — run v4_missed_trades_tracker.py"); return

        # ===== v1 BASELINE API endpoints =====
        if path == '/v1/api/v4/signals':
            self._serve_json(SIGNALS_BASELINE_FILE,
                "v4_signals_baseline.json not found yet — baseline engine hasn't run"); return
        if path == '/v1/api/v4/breakouts':
            # No baseline preposition fork yet — share the same data
            # (preposition_scanner has minimal Phase 1-5 changes; squeeze + sector_etfs
            # are additive, doesn't fundamentally differ from original)
            self._serve_json(BREAKOUTS_FILE, "preposition watchlist not found yet"); return
        if path == '/v1/api/v4/positions':
            # Baseline doesn't trade — return empty positions list
            self._send_empty({"positions": [], "_note": "baseline runs in shadow — no trades executed"})
            return
        if path == '/v1/api/v4/gaps':
            # Gap scanner is shared (no baseline fork)
            self._serve_json(GAPS_FILE, "v4_gap_watchlist.json not found yet"); return

        # ===== Static dashboard (HTML/CSS/JS) — serve from /v1/ or /v2/ prefix =====
        if path.startswith('/v1/') or path.startswith('/v2/'):
            # Strip the /v{n}/ prefix and let the static handler serve from there
            self.path = path[3:] or '/'
            return super().do_GET()

        super().do_GET()

    def _serve_positions(self, path):
        import json as _json
        positions = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    positions = _json.load(f)
            except Exception:
                positions = []
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps({"positions": positions}).encode())

    def _send_empty(self, payload):
        import json as _json
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(payload).encode())

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
            return
        super().send_error(code, message, explain)

    def _serve_json(self, path, missing_msg):
        if os.path.exists(path):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            import json as _json
            self.wfile.write(_json.dumps({"error": missing_msg, "candidates": [], "signals": [], "metadata": {}}).encode())


if __name__ == "__main__":
    print(f"🚀 V4 Dashboard Server live")
    print(f"   v2 (ENHANCED, production):  http://localhost:{PORT}/v2/")
    print(f"   v1 (BASELINE, shadow):      http://localhost:{PORT}/v1/")
    server = HTTPServer(('', PORT), V4DashboardServer)
    server.serve_forever()
