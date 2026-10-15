import json
import socket
import sys

import pytest

FAKE_SERVER = '''
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
http.server.HTTPServer((os.environ["KB_HOST"], int(os.environ["KB_PORT"])), H).serve_forever()
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def kb_paths(tmp_path, monkeypatch):
    """A fake kb checkout whose `kb.server` is a tiny HTTP server, and every path the service reads."""
    from awm.kb import instances

    checkout = tmp_path / "kb"
    (checkout / "src" / "kb").mkdir(parents=True)
    (checkout / "src" / "kb" / "__init__.py").write_text("")
    (checkout / "src" / "kb" / "server.py").write_text(FAKE_SERVER)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "sk-or-test"}}))
    port = _free_port()
    for name, value in {
        "CHECKOUT": checkout, "LIVE": checkout / "live", "SNAPSHOT_DIR": checkout / "data" / "store",
        "LOG_FILE": tmp_path / "state" / "logs" / "kb-server.log", "PORT": port,
        "URL": f"http://127.0.0.1:{port}", "AUTH_JSON": auth,
        "ZOTERO_LIBRARY": tmp_path / "library.json", "START_TIMEOUT_S": 20.0,
    }.items():
        monkeypatch.setattr(instances, name, value)
    monkeypatch.setenv("KB_PYTHON", sys.executable)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("KB_ZOTERO_LIBRARY", raising=False)
    return instances
