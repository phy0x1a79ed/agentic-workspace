"""A stand-in for alcor's vault API, signin frame and modals (CONTRACT.md § port-ops)."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ORIGIN = "http://127.0.0.1:37750"
CALLS: list[dict] = []
COOKIE = "awm_device=ok"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Security-Policy", f"frame-ancestors {ORIGIN}")

    def _send(self, code, body, ctype="application/json", extra=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, "")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/_calls":
            return self._send(200, json.dumps(CALLS))
        if u.path == "/signin":
            page = (f"<script>document.cookie='{COOKIE}; path=/; SameSite=Lax';"
                    f"parent.postMessage({{type:'awm-signed-in'}}, '{ORIGIN}');</script>signed in")
            return self._send(200, page, "text/html", [("Set-Cookie", f"{COOKIE}; Path=/; SameSite=Lax")])
        if u.path.startswith("/modal/approve/"):
            rid = u.path.rsplit("/", 1)[1]
            CALLS.append({"path": u.path, "modal": True})
            page = (f"<button id=go onclick=\"parent.postMessage({{type:'awm-approval',request_id:'{rid}',ok:true}},"
                    f"'{ORIGIN}')\">Approve {rid}</button>")
            return self._send(200, page, "text/html")
        if u.path == "/modal/twofa":
            CALLS.append({"path": self.path, "modal": True})
            page = ("<button id=go onclick=\"parent.postMessage({type:'awm-approval',ok:true},"
                    f"'{ORIGIN}')\">Arm</button>")
            return self._send(200, page, "text/html")
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if COOKIE not in (self.headers.get("Cookie") or ""):
            return self._send(401, json.dumps({"error": "sign in"}))
        CALLS.append({"path": self.path, "body": body, "origin": self.headers.get("Origin")})
        path = urlparse(self.path).path
        if path == "/api/agent":
            return self._send(200, json.dumps({"task_id": "stub-task-1", "order_id": "o1"}))
        if path == "/api/seat/remote_control":
            return self._send(200, json.dumps({"ok": True, "seat": body["seat"],
                                               "remote_control": body["on"], "live": "none"}))
        if path == "/api/seat/refresh":
            return self._send(200, json.dumps({"ok": True, "seat": body["seat"], "model": body["model"],
                                               "effort": body["effort"], "live": "pending"}))
        self._send(200, json.dumps({"ok": True}))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 37751
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
