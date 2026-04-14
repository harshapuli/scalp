import json
import os
import http.server
import socketserver
from urllib.parse import urlparse

PORT = 8080
DIRECTORY = "dashboard"

class LiveDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == '/api/live':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            
            try:
                # Up one level since server is run in live_engine_v2, 
                # but live_state.json is exported to live_engine_v2 root.
                if os.path.exists("live_state.json"):
                    with open("live_state.json", "r") as f:
                        data = f.read()
                        self.wfile.write(data.encode('utf-8'))
                else:
                    self.wfile.write(json.dumps({"error": "State file not created yet (Engine might be booting)."}).encode('utf-8'))
            except Exception as e:
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            return super().do_GET()

if __name__ == "__main__":
    # Ensure dashboard directory exists
    if not os.path.exists(DIRECTORY):
        os.makedirs(DIRECTORY)
        
    with socketserver.TCPServer(("", PORT), LiveDashboardHandler) as httpd:
        print(f"🚀 Live Dashboard Backend serving at http://localhost:{PORT}")
        print("Serving /api/live endpoint from live_state.json")
        httpd.serve_forever()
