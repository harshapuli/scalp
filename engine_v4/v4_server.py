import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8084
DIRECTORY = os.path.join(os.path.dirname(__file__), "dashboard")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "v4_signals.json")
BREAKOUTS_FILE = os.path.join(os.path.dirname(__file__), "v4_preposition_watchlist.json")
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "v4_paper_positions.json")
GAPS_FILE = os.path.join(os.path.dirname(__file__), "v4_gap_watchlist.json")
SIGNALS_BASELINE_FILE = os.path.join(os.path.dirname(__file__), "v4_signals_baseline.json")

class V4DashboardServer(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def do_GET(self):
        if self.path == '/api/v4/signals':
            self._serve_json(SIGNALS_FILE, "v4_signals.json not found yet")
            return
        if self.path == '/api/v4/breakouts':
            self._serve_json(BREAKOUTS_FILE,
                             "v4_preposition_watchlist.json not found yet — pre-position scanner hasn't run")
            return
        if self.path == '/api/v4/positions':
            self._serve_positions()
            return
        if self.path == '/api/v4/gaps':
            self._serve_json(GAPS_FILE,
                             "v4_gap_watchlist.json not found yet — gap scanner hasn't run")
            return
        if self.path == '/api/v4/signals_baseline':
            self._serve_json(SIGNALS_BASELINE_FILE,
                             "v4_signals_baseline.json not found yet — baseline engine hasn't run")
            return
        super().do_GET()

    def _serve_positions(self):
        """Return the positions file in the same wrapper shape as other APIs."""
        import json as _json
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as f:
                    positions = _json.load(f)
            except Exception:
                positions = []
        else:
            positions = []
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps({"positions": positions}).encode())

    def send_error(self, code, message=None, explain=None):
        """Override: redirect 404s on static paths to / (SPA behavior).
        Lets stale bookmarks / cached URLs like /mobile_app/index.html still land on the dashboard."""
        if code == 404:
            self.send_response(302)
            self.send_header('Location', '/')
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
            self.wfile.write(_json.dumps({"error": missing_msg, "candidates": [], "metadata": {}}).encode())

if __name__ == "__main__":
    print(f"🚀 V4 Enterprise Dashboard Server Live -> http://localhost:{PORT}")
    server = HTTPServer(('', PORT), V4DashboardServer)
    server.serve_forever()
