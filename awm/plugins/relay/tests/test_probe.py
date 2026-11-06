"""The relay's probe tells a live relay from an absent one, and counts what it holds."""

from __future__ import annotations

import http.server
import json
import threading

from awm.relay import hub_adapter


def _server(held: dict):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/health":
                body = b"ok\n"
            elif self.headers.get("Authorization") == "Bearer tok":
                body = json.dumps(held).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_probe_counts_held_rooms_and_waits_without_the_bearer(monkeypatch):
    srv = _server({"sessions": 1, "polled": 0, "rooms": 2})
    monkeypatch.setenv("AWM_TETHER_PORT", str(srv.server_address[1]))
    try:
        monkeypatch.setenv("AWM_TETHER_ISSUE_TOKEN", "tok")
        state, report = hub_adapter.probe_relay(timeout=2)
        assert state == "answering" and len(report["sessions"]) == 3
        monkeypatch.setenv("AWM_TETHER_ISSUE_TOKEN", "")
        assert hub_adapter.probe_relay(timeout=2)[0] == "busy"
    finally:
        srv.shutdown()
    assert hub_adapter.probe_relay(timeout=1)[0] == "absent"
