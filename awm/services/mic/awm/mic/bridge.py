"""The off-host HTTPS mic bridge (stdlib transport; one lazy gatewayclient call).

A stripped port of remote-audio's ``server.py``: serves the mic page and
accepts the browser mic as s16le PCM over a hand-rolled WebSocket, piping each
stream into the PulseAudio ``virtmic`` null-sink via ``pacat`` so the browser
becomes WSL's microphone. Bound ``0.0.0.0:<port>`` with TLS always on, since a
ZeroTier phone needs a secure context for ``getUserMedia`` and can't reach the
loopback-only awm hub.

Dropped vs the reference: the ``/voice/*`` tmux toggle, the ttyd port injection,
and the whole cockpit/session surface — this is the microphone, nothing else.

Single-stream policy: a new audio stream terminates the prior ``pacat`` so two
phones can't mix into the sink (the loser's WS sees a broken pipe and closes).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import ssl
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("awm.mic.bridge")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _State:
    """Live bridge status, read by the ``mic_status`` verb."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_streams = 0
        self.last_pcm_ts: float = 0.0
        self.port: int | None = None
        self.tls = False
        self.sink = "virtmic"
        self._pacat: subprocess.Popen | None = None  # the single allowed stream


STATE = _State()


def status() -> dict:
    """Snapshot of the bridge for the status verb."""
    with STATE.lock:
        return {
            "listener_port": STATE.port,
            "tls": STATE.tls,
            "sink": STATE.sink,
            "active_streams": STATE.active_streams,
            "last_pcm_ts": STATE.last_pcm_ts or None,
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Bound by serve() before the server starts.
    static_dir: str = ""
    ca_cert: str = ""
    sink: str = "virtmic"

    def log_message(self, *a):  # silence stdlib access logging
        pass

    # ---- static files ----
    def _serve(self, rel: str, ctype: str) -> None:
        import os

        try:
            with open(os.path.join(self.static_dir, rel), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._ws()
        if self.path in ("/", "/index.html"):
            return self._serve("index.html", "text/html; charset=utf-8")
        if self.path == "/worklet.js":
            return self._serve("worklet.js", "application/javascript")
        if self.path in ("/ca.crt", "/ca.pem"):
            return self._serve_ca()
        self.send_error(404)

    def _serve_ca(self) -> None:
        """Serve the local root CA so a device can install + trust it once,
        clearing ERR_CERT_AUTHORITY_INVALID. Sent as a downloadable
        ``application/x-x509-ca-cert`` so Android offers to install it."""
        try:
            with open(self.ca_cert, "rb") as f:
                body = f.read()
        except (FileNotFoundError, OSError):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header(
            "Content-Disposition", 'attachment; filename="remote-audio-ca.crt"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- websocket ----
    def _ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400)
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        log.info("ws connected %s", self.client_address)
        with STATE.lock:
            STATE.active_streams += 1

        pacat: subprocess.Popen | None = None
        frag_op, frag = None, bytearray()
        try:
            while True:
                fin, op, payload = self._read_frame()
                if op is None or op == 0x8:        # closed / close frame
                    break
                if op == 0x9:                      # ping -> pong
                    self._send_frame(0xA, payload)
                    continue
                if op == 0xA:                      # pong
                    continue
                if op == 0x0:                      # continuation
                    frag += payload
                    if not fin:
                        continue
                    op, payload, frag = frag_op, bytes(frag), bytearray()
                    frag_op = None
                elif not fin:                      # first fragment
                    frag_op, frag = op, bytearray(payload)
                    continue

                if op == 0x1:                       # text = control (config / ping)
                    try:
                        cfg = json.loads(payload.decode("utf-8"))
                    except Exception as e:  # noqa: BLE001
                        log.debug("bad control msg %s", e)
                        continue
                    if cfg.get("type") == "ping":
                        # echo back so the client can measure RTT
                        try:
                            self._send_frame(0x1, payload)
                        except OSError as e:
                            log.debug("ping echo failed %s", e)
                            break
                        continue
                    rate = int(cfg.get("sampleRate", 48000))
                    ch = int(cfg.get("channels", 1))
                    pacat = self._start_stream(rate, ch)
                    log.info("pacat started rate=%d ch=%d", rate, ch)
                elif op == 0x2:                     # binary PCM
                    if pacat and pacat.stdin:
                        try:
                            pacat.stdin.write(payload)
                        except (BrokenPipeError, ValueError):
                            log.info("pacat pipe broke (stream superseded?)")
                            break
                        with STATE.lock:
                            STATE.last_pcm_ts = time.time()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log.debug("ws error %s", e)
        finally:
            if pacat:
                try:
                    pacat.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                pacat.terminate()
            with STATE.lock:
                STATE.active_streams = max(0, STATE.active_streams - 1)
                if STATE._pacat is pacat:
                    STATE._pacat = None
            log.info("ws closed")

    def _ensure_sink(self) -> None:
        """Ask virtmic to guarantee the sink exists, right before we feed it.

        This is where the mic → virtmic dependency is made explicit instead of
        temporal. The gateway guarantees no start order between services, so
        provisioning at boot would race; ensuring here means the sink is
        present at the moment audio actually needs it. We're on the WS handler
        thread, never the event loop, so the blocking `call_sync` is correct.

        Best-effort: if virtmic is unreachable we still start `pacat`, because
        the sink may well already exist and refusing audio outright would be a
        worse failure than a possibly-dead one.
        """
        try:
            from awm import gatewayclient
            gatewayclient.call_sync("virtmic", "ensure", timeout=30.0)
        except Exception as e:  # noqa: BLE001
            log.warning("virtmic ensure failed (%s); starting pacat anyway", e)

    @staticmethod
    def _pacat_env() -> dict:
        """The environment `pacat` needs to find the user's PulseAudio socket.

        The gateway respawns services under systemd's minimal environment, which
        has no ``XDG_RUNTIME_DIR`` — and without it libpulse cannot locate
        ``$XDG_RUNTIME_DIR/pulse/native`` and fails with "Connection refused"
        even when the daemon is perfectly healthy. That mismatch is invisible
        from `virtmic_status`, which reports on *its own* repaired environment.

        This is about mic's subprocess environment, not about owning the audio
        plumbing, so it stays here rather than moving to virtmic.
        """
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        return env

    def _start_stream(self, rate: int, ch: int) -> subprocess.Popen:
        """Start a pacat for this stream, terminating any prior one first so
        only one phone ever feeds the sink."""
        self._ensure_sink()
        with STATE.lock:
            if STATE._pacat is not None:
                try:
                    STATE._pacat.terminate()
                except Exception:  # noqa: BLE001
                    pass
            cmd = [
                "pacat", "--playback", "-d", self.sink, "--raw",
                "--format=s16le", f"--rate={rate}", f"--channels={ch}",
                "--client-name=awm-mic", "--stream-name=browser-mic",
                "--latency-msec=40",
            ]
            pacat = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     env=self._pacat_env())
            STATE._pacat = pacat
            return pacat

    # ---- frame I/O ----
    def _read_exact(self, n: int):
        if n == 0:
            return b""
        data = self.rfile.read(n)
        if not data or len(data) < n:
            return None
        return data

    def _read_frame(self):
        hdr = self._read_exact(2)
        if not hdr:
            return (None, None, None)
        b0, b1 = hdr[0], hdr[1]
        fin = bool(b0 & 0x80)
        op = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if ln == 126:
            ext = self._read_exact(2)
            if ext is None:
                return (None, None, None)
            ln = struct.unpack(">H", ext)[0]
        elif ln == 127:
            ext = self._read_exact(8)
            if ext is None:
                return (None, None, None)
            ln = struct.unpack(">Q", ext)[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(ln) if ln else b""
        if payload is None:
            return (None, None, None)
        if masked and payload:
            payload = bytes(payload[i] ^ mask[i & 3] for i in range(len(payload)))
        return (fin, op, payload)

    def _send_frame(self, op: int, data: bytes = b"") -> None:
        b0 = 0x80 | op
        n = len(data)
        if n < 126:
            hdr = struct.pack(">BB", b0, n)
        elif n < 65536:
            hdr = struct.pack(">BBH", b0, 126, n)
        else:
            hdr = struct.pack(">BBQ", b0, 127, n)
        self.wfile.write(hdr + data)
        self.wfile.flush()


def serve(*, port: int, cert: str, key: str, ca: str,
          static_dir: str, sink: str = "virtmic") -> None:
    """Bind ``0.0.0.0:port`` with TLS and serve forever (blocks).

    Designed to run in a daemon thread launched from the hub adapter's
    ``on_start``. ``ThreadingHTTPServer`` sets ``allow_reuse_address`` so a
    gateway respawn rebinds the port cleanly.
    """
    Handler.static_dir = static_dir
    Handler.ca_cert = ca
    Handler.sink = sink
    with STATE.lock:
        STATE.port = port
        STATE.sink = sink
        STATE.tls = bool(cert)

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log.info("listening on https://0.0.0.0:%d (sink=%s, tls=on)", port, sink)
    srv.serve_forever()
