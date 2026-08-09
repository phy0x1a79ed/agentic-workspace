"""Inject into a background Claude Code session over the daemon's PTY socket.

The tmux backend exists because tmux owns the master side of an interactive
session's pty and will paste into it on request. A background session has a pty
too — it just belongs to ``claude bg-pty-host`` instead, which publishes it on a
unix socket. A pty has exactly one master, so these two reaches are disjoint by
construction: nothing tmux can do reaches a background session, and vice versa.

Wire format, per frame: a four-byte big-endian payload length, then one type
byte, then the payload. The length covers the payload **only** — it does not
include the type byte. Type ``0x01`` is a JSON control frame, ``0x00`` is raw pty
bytes (the app's output, and our keystrokes going the other way). Reading is
unauthenticated; *writing* is not, so a control frame carrying the session's
``ptyAuth`` must be sent before any raw frame or the input is dropped and an
``auth-required`` control frame comes back.

This is a private protocol, read out of the Claude Code bundle rather than a
documented interface. It is version-checked on connect so that a CLI update which
moves it degrades to a clear "background reflection unavailable" rather than
misfiring; the tmux backend is unaffected either way.
"""
from __future__ import annotations

import json
import logging
import socket as _socket
import struct
import threading
import time
from typing import Callable, Optional

from awm.reflection import session_target
from awm.reflection.guards import is_slash, refusal, resume_text

log = logging.getLogger("awm.reflection.daemon_inject")

# Frame types.
_RAW = 0x00
_CTL = 0x01

# The roster advertises the protocol it speaks. Anything else and we stop rather
# than write bytes into a socket whose framing we are guessing at.
SUPPORTED_PROTO = 1

# Bracketed paste (DEC private mode 2004). Not decoration: it is what makes a
# leading `/` arrive as literal text instead of opening the TUI's slash menu,
# which is the same reason the tmux backend passes `paste-buffer -p`.
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_BRACKETED_PASTE_MODE = 2004
_ENTER = b"\r"

# Settle beat between the paste landing and the Enter that submits it — the same
# beat, and for the same reason, as the tmux path's.
_SETTLE_S = 0.15

# How long to wait for the host's replay to finish and the session to go live
# before authenticating. The replay is the scrollback; we do not need it, we just
# need to not race the handshake.
_HELLO_WAIT_S = 5.0
_QUIET_S = 0.6

# Deferred follow-up, watching the session record's own `status` field rather
# than scraping the screen. Same shape as the tmux watcher and the same hazard:
# `/compact` runs at end of turn, so the sequence is busy → a brief idle → busy
# again while compacting → idle. Firing on the first idle would resume on the old
# context, which is the entire bug the deferred design exists to prevent — hence
# the confirmed idle streak.
#
# What marks the command as *started* is `statusUpdatedAt` moving past the moment
# we injected, not catching a sample that reads "busy". Requiring a busy sighting
# is a trap: a short conversation compacts in a couple of seconds and the busy
# window can close between two polls, after which the watcher waits out the full
# hard cap and the session sits idle for fifteen minutes. A timestamp cannot be
# missed that way — it is still there on the next poll, whenever that lands.
_POLL_S = 2.0
_FAST_POLL_S = 0.3       # until the session is seen to react at all
_IDLE_CONFIRM_POLLS = 3
_FOLLOWUP_MAX_WAIT_S = 900.0
_BUSY_STATES = {"busy"}
_SETTLED_STATES = {"idle", "waiting"}


class DaemonError(RuntimeError):
    """The daemon PTY socket could not be reached, or spoke an unexpected shape."""


Opener = Callable[[str], "_SocketLike"]


class _SocketLike:  # pragma: no cover - typing shim only
    def sendall(self, data: bytes) -> None: ...
    def recv(self, n: int) -> bytes: ...
    def settimeout(self, t: float) -> None: ...
    def close(self) -> None: ...


def _open_unix(path: str) -> _SocketLike:
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect(path)
    except OSError as exc:
        s.close()
        raise DaemonError(
            f"cannot reach the background session's PTY socket at {path}: {exc}"
        ) from None
    return s


def frame(kind: int, payload: bytes) -> bytes:
    """Encode one frame. The length prefix EXCLUDES the type byte."""
    return struct.pack(">I", len(payload)) + bytes([kind]) + payload


class _FrameReader:
    """Incremental frame decoder over a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out: list[tuple[int, bytes]] = []
        while len(self._buf) >= 5:
            (n,) = struct.unpack(">I", self._buf[:4])
            if len(self._buf) < 5 + n:
                break
            out.append((self._buf[4], bytes(self._buf[5:5 + n])))
            del self._buf[:5 + n]
        return out


class Connection:
    """A short-lived, authenticated connection to one session's PTY.

    Deliberately short-lived: the host pings periodically and drops a client that
    misses three, and holding a socket open across a whole compaction just to
    keep answering them buys nothing when the completion signal lives in a file.
    Pings that do arrive while we are connected are answered anyway.
    """

    def __init__(self, target: session_target.DaemonTarget, *,
                 opener: Opener = _open_unix,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._target = target
        self._sleep = sleep
        self._clock = clock
        self._reader = _FrameReader()
        self._sock = opener(target.sock)
        self._authed = False
        self.hello: dict = {}
        self.output = bytearray()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- plumbing ----------------------------------------------------------

    def _pump(self, *, until_live: bool = False, quiet_for: float = _QUIET_S,
              cap: float = _HELLO_WAIT_S) -> None:
        """Read frames until the stream goes quiet (or the session reports live).

        Raw pty bytes accumulate in ``self.output`` so callers can inspect what
        the screen is showing; control frames are handled here.
        """
        try:
            self._sock.settimeout(0.3)
        except OSError:
            pass
        start = last = self._clock()
        while self._clock() - start < cap:
            try:
                data = self._sock.recv(65536)
            except TimeoutError:
                if self._clock() - last > quiet_for:
                    return
                continue
            except OSError:
                return
            if not data:
                return
            last = self._clock()
            # One read can carry several frames. Finish the batch even once the
            # stop condition is met — returning from inside the loop would throw
            # away frames that are already decoded and unrecoverable, which for
            # a batch containing both `live` and the first screen paint means
            # silently losing the screen.
            done = False
            for kind, payload in self._reader.feed(data):
                if kind == _RAW:
                    self.output.extend(payload)
                    continue
                try:
                    ctl = json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                t = ctl.get("t")
                if t == "hello":
                    self.hello = ctl
                elif t == "ping":
                    self._send(_CTL, json.dumps({"t": "pong"}).encode())
                elif t == "auth-required":
                    raise DaemonError(
                        "the background session rejected our input token; it has "
                        "probably respawned since the roster was read")
                elif t == "live" and until_live:
                    done = True
            if done:
                return

    def _send(self, kind: int, payload: bytes) -> None:
        try:
            self._sock.sendall(frame(kind, payload))
        except OSError as exc:
            raise DaemonError(f"writing to the PTY socket failed: {exc}") from None

    # -- public ------------------------------------------------------------

    def handshake(self) -> None:
        """Read the host's greeting and check we understand what it speaks."""
        self._pump(until_live=True)
        version = self.hello.get("version")
        if self.hello and "replPid" not in self.hello:
            raise DaemonError(
                f"the background session's PTY host greeted us with an "
                f"unrecognised message (version {version!r}); reflection cannot "
                f"safely drive it — background reflection is unavailable until "
                f"this is re-verified against the current Claude Code build")

    def authenticate(self) -> None:
        self._send(_CTL, json.dumps(
            {"t": "auth", "token": self._target.auth}).encode())
        self._authed = True
        # Give the host a beat to register us before the first raw frame; an
        # unauthenticated raw frame is dropped silently apart from the
        # `auth-required` control frame we would then be racing.
        self._sleep(0.2)

    def type_text(self, text: str) -> None:
        """Deliver ``text`` as a bracketed paste."""
        if not self._authed:
            raise DaemonError("refusing to send input before authenticating")
        payload = text.encode("utf-8")
        if _BRACKETED_PASTE_MODE in self._target.dec_modes or not self._target.dec_modes:
            payload = _PASTE_START + payload + _PASTE_END
        else:
            log.warning("reflection: session %s does not report bracketed paste; "
                        "sending %r raw", self._target.session_id, text[:20])
        self._send(_RAW, payload)

    def press_enter(self) -> None:
        self._send(_RAW, _ENTER)

    def send_keys(self, data: bytes) -> None:
        """Send raw key bytes (not text) — e.g. a Shift+Tab escape sequence."""
        if not self._authed:
            raise DaemonError("refusing to send input before authenticating")
        self._send(_RAW, data)

    def screen(self, *, quiet_for: float = _QUIET_S,
               cap: float = _HELLO_WAIT_S) -> str:
        """Whatever the host has sent us so far, as text.

        This is the raw pty byte stream with escape sequences still in it, not a
        rendered screen — enough to substring-match TUI copy (which is what the
        tmux backend does against ``capture-pane`` output too), and it avoids
        putting a terminal emulator in the service's dependency set for that.
        """
        self._pump(quiet_for=quiet_for, cap=cap)
        return self.output.decode("utf-8", "replace")


def _connect(target: session_target.DaemonTarget, *, opener: Opener,
             sleep: Callable[[float], None]) -> Connection:
    conn = Connection(target, opener=opener, sleep=sleep)
    try:
        conn.handshake()
        conn.authenticate()
    except Exception:
        conn.close()
        raise
    return conn


def _paste_and_submit(text: str, target: session_target.DaemonTarget, *,
                      enter: bool, opener: Opener,
                      sleep: Callable[[float], None] = time.sleep) -> bool:
    with _connect(target, opener=opener, sleep=sleep) as conn:
        conn.type_text(text)
        if enter:
            sleep(_SETTLE_S)
            conn.press_enter()
            return True
    return False


# ---------------------------------------------------------------------------
# Deferred follow-up
# ---------------------------------------------------------------------------

def _status(repl_pid: int) -> Optional[tuple[str, int]]:
    """Return ``(status, statusUpdatedAt)`` for a background session, or ``None``."""
    try:
        record = json.loads(
            (session_target.SESSIONS_DIR / f"{repl_pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return str(record.get("status") or ""), int(record.get("statusUpdatedAt") or 0)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _await_and_followup(text: str, followup: str,
                        target: session_target.DaemonTarget, *,
                        opener: Opener = _open_unix,
                        injected_at_ms: Optional[int] = None,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic,
                        now_ms: Callable[[], int] = _now_ms) -> None:
    """Block until the injected command has finished, then submit ``followup``.

    Uses the session's own ``status`` field instead of reading the screen: it is
    structured, it is maintained by the session itself, and it does not go stale
    when TUI wording changes.

    Two conditions must both hold before the resume fires: the session must have
    *reacted* since we injected (its status timestamp moved past that moment),
    and it must then be settled — several consecutive idle samples, not the first
    one. Either condition alone is satisfiable by the brief lull that precedes
    compaction, which is exactly when resuming would land on the old context.
    """
    injected_at_ms = injected_at_ms if injected_at_ms is not None else now_ms()
    deadline = clock() + _FOLLOWUP_MAX_WAIT_S
    reacted = False
    idle_streak = 0
    ready = False
    while clock() < deadline:
        sample = _status(target.repl_pid)
        if sample is None:
            log.warning("reflection: background session %s vanished while "
                        "awaiting completion; no resume injected",
                        target.name or target.session_id)
            return
        status, updated_ms = sample
        if updated_ms > injected_at_ms:
            reacted = True
        if status in _BUSY_STATES:
            idle_streak = 0
        elif reacted and status in _SETTLED_STATES:
            idle_streak += 1
            if idle_streak >= _IDLE_CONFIRM_POLLS:
                ready = True
                break
        else:
            idle_streak = 0
        sleep(_POLL_S if reacted else _FAST_POLL_S)
    if not ready:
        log.warning("reflection: command %r completion not observed within %ss; "
                    "injecting resume anyway", text, _FOLLOWUP_MAX_WAIT_S)
    # Re-read the roster: a background session that respawned mid-wait has a new
    # PTY socket and a new input token, and the ones we were handed at injection
    # time are dead. Same session id, though — that is the durable identity.
    try:
        fresh = session_target.resolve(target.repl_pid)
    except session_target.ResolveError:
        fresh = target
    if not isinstance(fresh, session_target.DaemonTarget):
        fresh = target
    try:
        _paste_and_submit(followup, fresh, enter=True, opener=opener, sleep=sleep)
    except DaemonError as exc:
        log.warning("reflection: could not inject resume into background "
                    "session %s: %s", target.name or target.session_id, exc)


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="reflection-followup-bg", daemon=True).start()


def send(text: str, *, target: session_target.DaemonTarget, enter: bool = True,
         delay_ms: int = 0, confirm: bool = False,
         followup: Optional[str] = None,
         opener: Opener = _open_unix,
         sleep: Callable[[float], None] = time.sleep,
         spawn: Callable[[Callable[[], None]], object] = _default_spawn) -> dict:
    """Type ``text`` into a background session and (unless ``enter=False``) submit it.

    Mirrors :func:`awm.reflection.tmux_inject.send` — same guards, same deferred
    follow-up contract, same result shape — over a different transport.
    """
    refused = refusal(text, confirm=confirm)
    if refused is not None:
        return refused

    if delay_ms and delay_ms > 0:
        sleep(min(delay_ms, 25_000) / 1000.0)

    injected_at_ms = _now_ms()
    submitted = _paste_and_submit(text, target, enter=enter, opener=opener,
                                  sleep=sleep)

    followup_sent = None
    followup_deferred = False
    if submitted and is_slash(text):
        followup_sent = resume_text(followup)
        followup_deferred = True
        spawn(lambda: _await_and_followup(text, followup_sent, target,
                                          opener=opener,
                                          injected_at_ms=injected_at_ms,
                                          sleep=sleep))

    return {"ok": True, "session": target.name or target.session_id,
            "hosting": "background", "text": text,
            "submitted": submitted, "followup": followup_sent,
            "followup_deferred": followup_deferred}
