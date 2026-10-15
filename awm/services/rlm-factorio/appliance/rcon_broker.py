"""In-container RCON broker: one engine socket per connection key, no reply wait.

The realm runs this with ``docker exec -i <container> python3 -u -c <source>``,
so it ships with the realm and needs no image rebuild. It speaks newline-
delimited JSON on stdin/stdout:

    -> {"id": 7, "op": "lua", "conn": "<key>", "code": "<lua>"}
    <- {"id": 7, "ok": true, "output": "...", "cost_ms": 0.42}
    <- {"id": 7, "ok": false, "error": "...", "retryable": true}
    -> {"id": 8, "op": "close", "conn": "<key>"}

Each key (a seat's player name, or ``sys``) gets its own socket, opened on first
use. Requests on one socket are pipelined and matched to replies by packet id;
Factorio answers every command with exactly one packet, whatever its size, so a
reply is complete the moment it arrives. Never send the Source-style empty
sentinel packet: Factorio does not echo it, and the read hangs.

Every Lua command is wrapped, on its own first line, in a profiler and a pcall,
then re-raised, so the engine still reports its own "Cannot execute command.
Error: ..." text and a script's line numbers are unchanged. The profiler line is
stripped from the output and returned as ``cost_ms``.

The broker exits when stdin closes, which is how it follows the realm process
that started it.
"""

import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time

AUTH, AUTH_RESPONSE, EXECCOMMAND = 3, 2, 2
COST_MARK = "\x1eCOST "
COST_RE = re.compile(r"\x1eCOST [^\n]*?([0-9.]+)\s*ms\n?")
WRAP_HEAD = ("local __gb_p=helpers.create_profiler() "
             "local __gb_ok,__gb_e=pcall(function(...) ")
WRAP_TAIL = ("\nend) __gb_p.stop() rcon.print({\"\", \"\\30COST \", __gb_p}) "
             "if not __gb_ok then error(__gb_e, 0) end")
CONNECT_TIMEOUT = 10.0
WARMUP_S = 15.0
CLOSE_GRACE_S = 10.0

_out_lock = threading.Lock()


def emit(msg):
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    with _out_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def engine_endpoint():
    """Port and password from the engine's own command line."""
    me = str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].endswith(b"/factorio") and b"--rcon-password" in argv:
            port = int(argv[argv.index(b"--rcon-port") + 1])
            return port, argv[argv.index(b"--rcon-password") + 1].decode()
    raise ConnectionError("engine not running")


def wrap(code):
    return "/silent-command " + WRAP_HEAD + code + WRAP_TAIL


def split_cost(text):
    m = COST_RE.search(text)
    if not m:
        return text, None
    return text[:m.start()] + text[m.end():], float(m.group(1))


class Conn:
    """One authenticated engine socket with pipelined, id-matched requests."""

    def __init__(self, key):
        self.key = key
        self.sock = None
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 100

    def _send(self, req_id, typ, body):
        payload = struct.pack("<ii", req_id, typ) + body.encode("utf-8") + b"\0\0"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("engine closed the connection")
            buf += chunk
        return buf

    def _recv(self, sock):
        length = struct.unpack("<i", self._recv_exact(sock, 4))[0]
        payload = self._recv_exact(sock, length)
        req_id, typ = struct.unpack("<ii", payload[:8])
        return req_id, typ, payload[8:-2].decode("utf-8", "replace")

    def _open(self):
        port, password = engine_endpoint()
        sock = socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT)
        self.sock = sock
        self._send(1, AUTH, password)
        while True:
            req_id, typ, _ = self._recv(sock)
            if typ == AUTH_RESPONSE:
                if req_id == -1:
                    raise ConnectionError("rcon auth rejected")
                break
        # The first command after an engine (re)start can come back empty, so a
        # socket is only handed out once a string round-trips.
        deadline = time.monotonic() + WARMUP_S
        while True:
            self._send(2, EXECCOMMAND, '/silent-command rcon.print("ready")')
            if self._recv(sock)[2].strip() == "ready":
                break
            if time.monotonic() > deadline:
                raise ConnectionError("rcon warm-up never answered")
            time.sleep(0.25)
        sock.settimeout(None)
        threading.Thread(target=self._reader, args=(sock,), daemon=True).start()

    def _reader(self, sock):
        try:
            while True:
                req_id, _typ, body = self._recv(sock)
                with self.lock:
                    rid = self.pending.pop(req_id, None)
                if rid is None:
                    continue
                output, cost = split_cost(body)
                emit({"id": rid, "ok": True, "output": output, "cost_ms": cost})
        except Exception as exc:  # noqa: BLE001
            self._fail(sock, f"engine connection lost ({exc}); retry")

    def _fail(self, sock, reason):
        with self.lock:
            if self.sock is sock:
                self.sock = None
            lost, self.pending = self.pending, {}
        try:
            sock.close()
        except OSError:
            pass
        for rid in lost.values():
            emit({"id": rid, "ok": False, "error": reason, "retryable": True})

    def submit(self, rid, code):
        with self.lock:
            if self.sock is None:
                try:
                    self._open()
                except Exception as exc:  # noqa: BLE001
                    if self.sock is not None:
                        self.sock.close()
                        self.sock = None
                    emit({"id": rid, "ok": False, "retryable": True,
                          "error": f"engine not reachable ({exc}); retry"})
                    return
            self.next_id = self.next_id % 0x7FFFFFF0 + 1
            req_id = self.next_id + 100
            self.pending[req_id] = rid
            sock = self.sock
            try:
                self._send(req_id, EXECCOMMAND, wrap(code))
                return
            except OSError as exc:
                failure = f"engine connection lost ({exc}); retry"
        self._fail(sock, failure)

    def close(self, grace=CLOSE_GRACE_S):
        """Close once in-flight replies land, or after ``grace`` seconds."""
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self.lock:
                if not self.pending:
                    break
            time.sleep(0.05)
        with self.lock:
            sock = self.sock
        if sock is not None:
            self._fail(sock, "connection closed")


def main():
    conns = {}
    work = {}

    def worker(key, q):
        conn = conns[key]
        while True:
            msg = q.get()
            if msg is None:
                return
            if msg.get("op") == "close":
                conn.close()
                emit({"id": msg.get("id"), "ok": True, "output": ""})
            else:
                conn.submit(msg.get("id"), msg.get("code") or "")

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        op = msg.get("op")
        if op == "ping":
            emit({"id": msg.get("id"), "ok": True, "output": "pong"})
            continue
        key = str(msg.get("conn") or "sys")
        if op not in ("lua", "close"):
            emit({"id": msg.get("id"), "ok": False, "error": f"unknown op {op!r}"})
            continue
        if key not in conns:
            conns[key] = Conn(key)
            work[key] = queue.Queue()
            threading.Thread(target=worker, args=(key, work[key]), daemon=True).start()
        work[key].put(msg)
    for conn in conns.values():
        conn.close(grace=0)
    os._exit(0)


if __name__ == "__main__":
    main()
