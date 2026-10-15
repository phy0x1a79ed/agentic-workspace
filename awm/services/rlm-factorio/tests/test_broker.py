import importlib.util
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from awm.rlm_factorio import broker

BROKER_PATH = Path(__file__).resolve().parents[1] / "appliance" / "rcon_broker.py"


def load_broker_module():
    spec = importlib.util.spec_from_file_location("rcon_broker", BROKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeFactorio:
    """Speaks just enough Factorio RCON: auth, then one reply packet per command."""

    def __init__(self, reply):
        self.reply = reply
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            conn, _ = self.srv.accept()
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn):
        head = conn.recv(4, socket.MSG_WAITALL)
        if len(head) < 4:
            return None
        payload = conn.recv(struct.unpack("<i", head)[0], socket.MSG_WAITALL)
        rid, typ = struct.unpack("<ii", payload[:8])
        return rid, typ, payload[8:-2].decode()

    @staticmethod
    def _send(conn, rid, typ, body):
        payload = struct.pack("<ii", rid, typ) + body.encode() + b"\0\0"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    def _serve(self, conn):
        while True:
            msg = self._recv(conn)
            if msg is None:
                return
            rid, typ, body = msg
            if typ == 3:
                self._send(conn, rid if body == "pw" else -1, 2, "")
            elif "rcon.print(\"ready\")" in body and "__gb_p" not in body:
                self._send(conn, rid, 0, "ready\n")
            else:
                self._send(conn, rid, 0, self.reply(body))


@pytest.fixture()
def rcon_mod(monkeypatch):
    mod = load_broker_module()
    out = []
    monkeypatch.setattr(mod, "emit", out.append)
    return mod, out


def wait_for(out, n, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    return out


def test_wrap_keeps_line_one_and_varargs():
    mod = load_broker_module()
    code = mod.wrap("rcon.print(select('#', ...))\nrcon.print(2)")
    assert code.startswith("/silent-command local __gb_p")
    assert "function(...) rcon.print(select('#', ...))\nrcon.print(2)\nend)" in code


def test_split_cost_strips_marker():
    mod = load_broker_module()
    text, cost = mod.split_cost("a\n\x1eCOST Duration: 0.064635ms\nCannot execute command. Error: x\n")
    assert text == "a\nCannot execute command. Error: x\n" and cost == pytest.approx(0.064635)
    assert mod.split_cost("syntax error\n") == ("syntax error\n", None)


def test_pipelined_replies_matched_and_costed(rcon_mod, monkeypatch):
    mod, out = rcon_mod
    fake = FakeFactorio(lambda body: "ok:" + body[-60:-40] + "\n\x1eCOST Duration: 1.5ms\n")
    monkeypatch.setattr(mod, "engine_endpoint", lambda: (fake.port, "pw"))
    conn = mod.Conn("seat-a")
    for i in range(10):
        conn.submit(i, f"rcon.print({i})")
    wait_for(out, 10)
    assert sorted(r["id"] for r in out) == list(range(10))
    assert all(r["ok"] and r["cost_ms"] == 1.5 for r in out)
    assert fake.connections == 1


def test_close_waits_for_inflight_replies(rcon_mod, monkeypatch):
    mod, out = rcon_mod
    fake = FakeFactorio(lambda body: (time.sleep(0.2), "late\n")[1])
    monkeypatch.setattr(mod, "engine_endpoint", lambda: (fake.port, "pw"))
    conn = mod.Conn("seat-a")
    conn.submit(1, "x")
    conn.close(grace=5)
    assert out and out[0]["ok"] and out[0]["output"] == "late\n"


def test_bad_password_is_retryable_error(rcon_mod, monkeypatch):
    mod, out = rcon_mod
    fake = FakeFactorio(lambda body: "")
    monkeypatch.setattr(mod, "engine_endpoint", lambda: (fake.port, "wrong"))
    mod.Conn("k").submit(1, "x")
    assert out[0]["ok"] is False and out[0]["retryable"] is True


FAKE_BROKER = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if m["code"] == "die":
        sys.exit(3)
    if m["code"] == "hang":
        continue
    print(json.dumps({"id": m["id"], "ok": True, "output": m["conn"] + ":" + m["code"], "cost_ms": 0.5}), flush=True)
'''


@pytest.fixture()
def fake_broker(monkeypatch):
    b = broker.Broker("test")
    monkeypatch.setattr(b, "_command", lambda src: [sys.executable, "-u", "-c", FAKE_BROKER])
    yield b
    b.stop()


def test_realm_client_concurrent_calls(fake_broker):
    results = {}

    def call(i):
        results[i] = fake_broker.lua(f"seat-{i % 3}", str(i), timeout=5)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[i]["output"] == f"seat-{i % 3}:{i}" for i in range(20))


def test_realm_client_timeout_is_not_retryable(fake_broker):
    with pytest.raises(broker.BrokerError) as exc:
        fake_broker.lua("k", "hang", timeout=0.3)
    assert exc.value.retryable is False and "may still run" in str(exc.value)


def test_realm_client_restarts_after_broker_exit(fake_broker):
    with pytest.raises(broker.BrokerError) as exc:
        fake_broker.lua("k", "die", timeout=5)
    assert exc.value.retryable is True
    assert fake_broker.lua("k", "again", timeout=5)["output"] == "k:again"
