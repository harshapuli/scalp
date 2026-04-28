"""scripts/dashboard_proxy.py — Reverse proxy: engine_v4 + scalp2 behind one URL.

Lets the user keep the existing pinggy tunnel (https://nktdoglohq.a.pinggy.link)
and access scalp2 at the same URL via a /scalp/* prefix.

Routing:
  /scalp                    → 302 redirect to /scalp/trade.html
  /scalp/*                  → localhost:8766 (scalp2), prefix stripped
  everything else           → localhost:8084 (engine_v4)

HTML transformations applied to responses:
  · scalp2 HTML responses get <base href="/scalp/"> injected after <head>
    so all relative URLs in the page resolve back through the proxy
  · engine_v4 HTML responses get a floating "🚀 Scalp" button injected
    at top-right so the user can navigate from any conviction page

Usage:
  $ python3 scripts/dashboard_proxy.py [--port 8088]

After it's running, re-point your pinggy tunnel from 8084 → 8088:
  $ ssh -p 443 -R0:localhost:8088 a.pinggy.io
  (or whichever pinggy command you use)
"""
from __future__ import annotations

import argparse
import http.server
import http.client
import socketserver
import sys
from pathlib import Path

# Don't import scalp2 stuff — this is a standalone proxy
SCALP_PREFIX = "/scalp"
SCALP_BACKEND = ("localhost", 8766)
V4_BACKEND = ("localhost", 8084)

# HTML to inject on scalp2 responses (right after <head>)
SCALP_HEAD_INJECT = '<base href="/scalp/">'

# HTML to inject on engine_v4 responses (right before </body>).
# Injects JS that adds a "Scalp" tab into the existing .topnav so it
# matches the look of Today/Triggered/Watchlist/Catalyst/Live. Falls back
# to a floating button if no .topnav element is found on the page.
V4_BODY_INJECT = """
<style>
  #_scalp_floating {
    position: fixed; top: 10px; right: 14px; z-index: 9999;
    padding: 8px 14px; border-radius: 999px;
    background: linear-gradient(135deg, #d97757, #e88e6f); color: #fff;
    text-decoration: none; font-family: 'Poppins', Arial, sans-serif;
    font-weight: 600; font-size: 12px; letter-spacing: 0.04em;
    box-shadow: 0 4px 14px rgba(217, 119, 87, 0.35);
    border: none; cursor: pointer; display: none;
  }
</style>
<a id="_scalp_floating" href="/scalp/trade.html" target="_blank" rel="noopener">🚀 Scalp</a>
<script>
(function() {
  function injectScalpNav() {
    var nav = document.querySelector('.topnav');
    if (nav && !nav.querySelector('a[href^="/scalp"]')) {
      // Match the pattern of existing nav links (look at first <a> for class/structure)
      var first = nav.querySelector('a');
      if (first) {
        var link = document.createElement('a');
        link.href = '/scalp/trade.html';
        link.target = '_blank';      // new tab
        link.rel = 'noopener';
        link.className = first.className.replace(/active/g, '').trim();
        // Match the inner structure: <span>label</span><span class="sub">sublabel</span>
        var sub = first.querySelector('.sub');
        if (sub) {
          link.innerHTML = '<span>🚀 Scalp</span><span class="sub">paper trader</span>';
        } else {
          link.textContent = '🚀 Scalp';
        }
        nav.appendChild(link);
      }
    } else if (!nav) {
      // No topnav — show the floating button instead
      var btn = document.getElementById('_scalp_floating');
      if (btn) btn.style.display = 'inline-flex';
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectScalpNav);
  } else {
    injectScalpNav();
  }
})();
</script>
"""


class _DashProxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Quiet — only log errors
        try:
            code = int(args[1]) if len(args) >= 2 else 0
        except (ValueError, IndexError):
            code = 0
        if code and code >= 500:
            sys.stderr.write(f"[proxy] {args[0]} → {args[1]}\n")

    def _proxy(self, method: str) -> None:
        path = self.path
        # Decide backend
        if path == SCALP_PREFIX or path == SCALP_PREFIX + "/":
            self.send_response(302)
            self.send_header("Location", "/scalp/trade.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith(SCALP_PREFIX + "/"):
            backend = SCALP_BACKEND
            forward_path = path[len(SCALP_PREFIX):]      # strip prefix
            inject_html_kind = "scalp"
        else:
            backend = V4_BACKEND
            forward_path = path
            inject_html_kind = "v4"

        # Read request body if any
        body = b""
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(int(cl))
            except Exception:
                body = b""

        # Strip hop-by-hop headers
        skip_hdrs = {"host", "connection", "transfer-encoding",
                     "keep-alive", "proxy-authenticate",
                     "proxy-authorization", "te", "trailers", "upgrade"}
        fwd_hdrs = {}
        for k, v in self.headers.items():
            if k.lower() not in skip_hdrs:
                fwd_hdrs[k] = v

        try:
            conn = http.client.HTTPConnection(backend[0], backend[1], timeout=15)
            conn.request(method, forward_path, body=body or None, headers=fwd_hdrs)
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = list(resp.getheaders())
            status = resp.status
            reason = resp.reason
            content_type = resp.getheader("Content-Type", "") or ""
            conn.close()
        except (ConnectionRefusedError, OSError) as e:
            self.send_response(502)
            msg = f"<h1>Bad Gateway</h1><p>Could not reach {backend[0]}:{backend[1]} — {e}</p>".encode()
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        # HTML transformations
        if "text/html" in content_type and resp_body:
            try:
                html = resp_body.decode("utf-8", errors="ignore")
                if inject_html_kind == "scalp":
                    if "<head>" in html and SCALP_HEAD_INJECT not in html:
                        html = html.replace("<head>", "<head>" + SCALP_HEAD_INJECT, 1)
                else:
                    # Inject floating Scalp button right before </body>
                    if "</body>" in html and "_scalp_btn" not in html:
                        html = html.replace("</body>", V4_BODY_INJECT + "</body>", 1)
                resp_body = html.encode("utf-8")
                # Update content-length below
            except Exception:
                pass

        # Rewrite Location headers for redirects from scalp2 backend
        new_headers = []
        for k, v in resp_headers:
            kl = k.lower()
            if kl == "content-length":
                new_headers.append(("Content-Length", str(len(resp_body))))
                continue
            if kl == "location" and inject_html_kind == "scalp" and v.startswith("/"):
                new_headers.append((k, SCALP_PREFIX + v))
                continue
            if kl in ("transfer-encoding", "connection"):
                continue
            new_headers.append((k, v))

        self.send_response(status, reason)
        for k, v in new_headers:
            self.send_header(k, v)
        # Make sure Content-Length is present even if backend didn't send it
        if not any(h[0].lower() == "content-length" for h in new_headers):
            self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(resp_body)

    def do_GET(self): self._proxy("GET")
    def do_POST(self): self._proxy("POST")
    def do_PUT(self): self._proxy("PUT")
    def do_DELETE(self): self._proxy("DELETE")
    def do_HEAD(self): self._proxy("HEAD")
    def do_OPTIONS(self): self._proxy("OPTIONS")


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088,
                    help="Proxy listen port (default 8088)")
    args = ap.parse_args()

    print(f"[proxy] starting on http://localhost:{args.port}")
    print(f"[proxy] /scalp/* → localhost:{SCALP_BACKEND[1]} (scalp2)")
    print(f"[proxy] /*       → localhost:{V4_BACKEND[1]} (engine_v4)")
    print()
    print(f"To expose via existing pinggy tunnel:")
    print(f"  1. Stop current pinggy tunnel (the one pointing at localhost:{V4_BACKEND[1]})")
    print(f"  2. Restart pinggy pointing at localhost:{args.port}")
    print(f"     e.g.  ssh -p 443 -R0:localhost:{args.port} a.pinggy.io")
    print()
    print(f"Then access:")
    print(f"  https://nktdoglohq.a.pinggy.link/conviction      ← engine_v4 (unchanged)")
    print(f"  https://nktdoglohq.a.pinggy.link/scalp/trade.html ← scalp2")
    print(f"  Floating 🚀 Scalp button on engine_v4 pages → links to scalp")
    print()

    with _ReusableServer(("", args.port), _DashProxy) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[proxy] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
