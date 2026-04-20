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
import requests
from http.server import HTTPServer, SimpleHTTPRequestHandler

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))
except Exception:
    pass

ALPACA_TRADING_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
}

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
ACCUMULATION_FILE = os.path.join(BASE_DIR, "v4_accumulation_signals.json")
ACCUMULATION_SUCCESS_FILE = os.path.join(BASE_DIR, "v4_accumulation_success.json")


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
        if path in ('/api/v4/live_account', '/v2/api/v4/live_account', '/v1/api/v4/live_account'):
            self._serve_live_alpaca(); return
        if path in ('/api/v4/preposition_history', '/v2/api/v4/preposition_history', '/v1/api/v4/preposition_history'):
            self._serve_preposition_history(); return
        if path in ('/api/v4/accumulation', '/v2/api/v4/accumulation', '/v1/api/v4/accumulation'):
            self._serve_json(ACCUMULATION_FILE,
                "v4_accumulation_signals.json not found yet — preposition scanner hasn't run with B1 yet"); return
        if path in ('/api/v4/accumulation_success', '/v2/api/v4/accumulation_success', '/v1/api/v4/accumulation_success'):
            self._serve_json(ACCUMULATION_SUCCESS_FILE,
                "v4_accumulation_success.json not found yet — run v4_accumulation_tracker.py"); return

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

    def _serve_preposition_history(self):
        """Returns triggered preposition setups across today + archive.
        Format: {triggered_today: [...], triggered_past: [...]}
        Each record: {ticker, direction, score, trigger_level, triggered_at_utc, date, tier}"""
        import json as _json, glob
        out = {'triggered_today': [], 'triggered_past': []}

        # Today's watchlist
        today_path = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')
        if os.path.exists(today_path):
            try:
                with open(today_path) as f: data = _json.load(f)
                for c in data.get('candidates', []):
                    if c.get('triggered_at_utc'):
                        out['triggered_today'].append({
                            'ticker': c.get('ticker'),
                            'direction': c.get('direction'),
                            'score': c.get('score'),
                            'tier': c.get('tier'),
                            'trigger_level': (c.get('key_levels') or {}).get('trigger_above'),
                            'triggered_at_utc': c.get('triggered_at_utc'),
                            'narrative': c.get('narrative', '')[:200],
                            'date': (data.get('metadata', {}).get('scan_completed_utc') or '')[:10],
                        })
            except Exception: pass

        # Archived days
        archive_dir = os.path.join(BASE_DIR, 'v4_preposition_archive')
        if os.path.isdir(archive_dir):
            for p in sorted(glob.glob(os.path.join(archive_dir, '*.json')), reverse=True):
                try:
                    with open(p) as f: data = _json.load(f)
                    scan_date = (data.get('metadata', {}).get('scan_completed_utc') or '')[:10]
                    for c in data.get('candidates', []):
                        if c.get('triggered_at_utc'):
                            out['triggered_past'].append({
                                'ticker': c.get('ticker'),
                                'direction': c.get('direction'),
                                'score': c.get('score'),
                                'tier': c.get('tier'),
                                'trigger_level': (c.get('key_levels') or {}).get('trigger_above'),
                                'triggered_at_utc': c.get('triggered_at_utc'),
                                'narrative': c.get('narrative', '')[:200],
                                'date': scan_date,
                            })
                except Exception: continue

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_live_alpaca(self):
        """Live snapshot of the entire Alpaca paper account — both bots' positions.
        Cross-references each Alpaca position with our local file to mark which
        ones are 'ours' vs 'other'."""
        import json as _json
        try:
            r = requests.get(f"{ALPACA_TRADING_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=10)
            positions = r.json() if r.status_code == 200 else []
            r2 = requests.get(f"{ALPACA_TRADING_URL}/v2/account", headers=ALPACA_HEADERS, timeout=10)
            account = r2.json() if r2.status_code == 200 else {}
        except Exception as e:
            self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
            self.wfile.write(_json.dumps({"error": str(e), "positions": []}).encode())
            return

        # Build set of our local symbols
        our_symbols = set()
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as f: ours = _json.load(f)
                our_symbols = {p.get('contract_symbol') for p in ours
                               if p.get('status') in ('OPEN', 'PENDING_ENTRY')}
            except Exception: pass

        # Tag each Alpaca position with origin
        enriched = []
        for p in positions:
            sym = p.get('symbol', '')
            origin = 'claude_v4' if sym in our_symbols else 'other_bot'
            enriched.append({
                'symbol': sym,
                'qty': int(p.get('qty', 0)),
                'avg_entry_price': float(p.get('avg_entry_price', 0)),
                'current_price': float(p.get('current_price', 0)),
                'market_value': float(p.get('market_value', 0)),
                'unrealized_pl': float(p.get('unrealized_pl', 0)),
                'unrealized_plpc': float(p.get('unrealized_plpc', 0)),
                'side': p.get('side'),
                'origin': origin,
            })
        # Sort by unrealized P&L descending
        enriched.sort(key=lambda x: -x['unrealized_pl'])

        # Aggregate stats
        total_unrealized = sum(p['unrealized_pl'] for p in enriched)
        ours = [p for p in enriched if p['origin'] == 'claude_v4']
        other = [p for p in enriched if p['origin'] == 'other_bot']

        payload = {
            'account': {
                'account_number': account.get('account_number'),
                'cash': float(account.get('cash', 0)) if account.get('cash') else 0,
                'equity': float(account.get('equity', 0)) if account.get('equity') else 0,
                'buying_power': float(account.get('buying_power', 0)) if account.get('buying_power') else 0,
                'options_buying_power': float(account.get('options_buying_power', 0)) if account.get('options_buying_power') else 0,
            },
            'summary': {
                'total_positions': len(enriched),
                'claude_v4_positions': len(ours),
                'other_bot_positions': len(other),
                'total_unrealized_pl': round(total_unrealized, 2),
                'claude_v4_unrealized_pl': round(sum(p['unrealized_pl'] for p in ours), 2),
                'other_bot_unrealized_pl': round(sum(p['unrealized_pl'] for p in other), 2),
            },
            'positions': enriched,
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
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
