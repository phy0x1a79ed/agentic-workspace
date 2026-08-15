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

What this module can and cannot promise is worth stating plainly, because the
gap is structural rather than a shortcoming to be closed later. Injection can
confirm that the frames were written to a socket whose host identified itself as
the calling session's, that the host did not reject them, and — by reading the
pty stream back — that the text is on screen in the prompt. It can NOT confirm
that the command ran: a slash command runs at end of turn, and the reflection
call has to *return* for the caller's turn to end. At the moment of the return
there is by construction nothing to observe yet. That is the whole reason the
follow-up is deferred to a watcher instead of awaited here, and anything that
later looks like a way to verify *execution* inline is this constraint being
forgotten. Verifying the keystrokes landed is a different and achievable claim;
do not let the two blur together.

This module knows the protocol and nothing else. Which session to reach is
:mod:`awm.reflection.session_target`'s job, retrying is
:mod:`awm.reflection.inject`'s, and waiting for completion is
:mod:`awm.reflection.watcher`'s.
"""
from __future__ import annotations

import json
import logging
import socket as _socket
import struct
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from awm.reflection import session_target

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

# How long to keep listening after the last write before closing the socket. Long
# enough for the host to say it threw the input away, short enough that it does
# not show up as latency on a verb the caller is blocking on. It is bounded by the
# cap rather than by the quiet period: a live session is streaming its own output
# the whole time, so "quiet" is not something this window can rely on reaching.
_ACCEPT_CHECK_S = 0.5
_ACCEPT_QUIET_S = 0.15

# Ctrl-U: kill the prompt line. Only ever sent before a *retry*, to wipe whatever
# a failed attempt left in the box so the next paste cannot concatenate onto it.
_CLEAR_KEY = b"\x15"


class DaemonError(RuntimeError):
    """The daemon PTY socket could not be reached, or spoke an unexpected shape.

    This used to carry a ``wrote_input`` flag so the retry above could *guess*
    whether bytes might already have landed and skip re-pasting if so. It is
    gone: the sender now reads the prompt back before pressing Enter, which
    answers that question directly instead of inferring it, and it retries only
    from before the commit point where a repeat cannot double-submit.
    """


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

    def __init__(self, target: session_target.DaemonLane, *,
                 opener: Opener = _open_unix,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._target = target
        self._sleep = sleep
        self._clock = clock
        self._reader = _FrameReader()
        self._sock = opener(target.sock)
        self._authed = False
        self._raw_sent = False
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
        """Read the host's greeting, and check it is the right session's host.

        The greeting names the REPL on the far end of the socket. Reflection
        already knows which REPL it is acting for, so comparing the two is the
        one place a wrong target can be caught before any keystroke is written —
        and a wrong target is reachable: the roster hands out recycled
        ``spare/*.pty.sock`` paths and its entries carry an attempt counter, so
        "the socket you were told about now belongs to another job" is a state
        the daemon reaches on its own, without anything being broken.

        No greeting at all is a failure too. It used to pass, because the
        unrecognised-message check was guarded on having *received* something —
        so a socket that connected and then said nothing sailed through, and
        authentication proceeded into a host that had never identified itself.
        """
        self._pump(until_live=True)
        if not self.hello:
            raise DaemonError(
                f"the background session's PTY host never greeted us on "
                f"{self._target.sock}; the roster entry is stale, or that socket "
                f"is no longer being served — refusing to type into it blind")
        version = self.hello.get("version")
        if "replPid" not in self.hello:
            raise DaemonError(
                f"the background session's PTY host greeted us with an "
                f"unrecognised message (version {version!r}); reflection cannot "
                f"safely drive it — background reflection is unavailable until "
                f"this is re-verified against the current Claude Code build")
        greeted = self.hello.get("replPid")
        if greeted != self._target.repl_pid:
            raise DaemonError(
                f"the PTY socket {self._target.sock} is hosting REPL {greeted}, "
                f"not the calling session's REPL {self._target.repl_pid}; the "
                f"roster entry is stale (the session was re-hosted, or a "
                f"pre-warmed socket has since been handed to another job) — "
                f"refusing to type into a different session")

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
        if not self._authed:
            raise DaemonError("refusing to send input before authenticating")
        self._send(_RAW, _ENTER)

    def check_not_rejected(self, *, cap: float = _ACCEPT_CHECK_S) -> None:
        """Raise if the host says it discarded what we just wrote.

        A **negative** check, and only that. An unauthenticated raw frame is
        dropped silently apart from the ``auth-required`` control frame that
        follows it — which the write path used to miss entirely, because it
        closed the socket the instant the last byte went out and never read
        again. Listening for a beat turns that class of silent no-op into a
        failure the caller (and the retry above it) can act on.

        It is not, and must not become, a check that the line was *accepted*.
        There is no positive acknowledgement in this protocol, and the session is
        mid-turn by design, so its prompt does not behave like an idle one. See
        the module docstring for why waiting for evidence the command ran is not
        something this call can do.
        """
        self._pump(quiet_for=_ACCEPT_QUIET_S, cap=cap)

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

    # -- the lane verbs ----------------------------------------------------
    # The same four :mod:`awm.reflection.inject` drives the tmux lane through.
    # It must not be able to tell which one it is holding.

    @property
    def label(self) -> str:
        return (f"background session "
                f"{self._target.name or self._target.session_id}")

    def read_back(self) -> str:
        """What the host has painted, bounded tightly.

        A live session streams its own output continuously, so this cannot wait
        for the stream to fall quiet the way the handshake can — it takes a short
        window and works with what arrives. Pumping is also what surfaces an
        ``auth-required``, so every read doubles as the rejection check.
        """
        return self.screen(quiet_for=_ACCEPT_QUIET_S, cap=_ACCEPT_CHECK_S)

    def clear(self) -> None:
        self.send_keys(_CLEAR_KEY)
        self._sleep(_SETTLE_S)

    def write(self, text: str) -> None:
        self.type_text(text)
        self._sleep(_SETTLE_S)

    def commit(self) -> None:
        self.press_enter()
        self.check_not_rejected()


def _connect(target: session_target.DaemonLane, *, opener: Opener,
             sleep: Callable[[float], None] = time.sleep) -> Connection:
    conn = Connection(target, opener=opener, sleep=sleep)
    try:
        conn.handshake()
        conn.authenticate()
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def open_lane(lane: session_target.DaemonLane, *, opener: Opener = _open_unix,
              sleep: Callable[[float], None] = time.sleep
              ) -> Iterator[Connection]:
    """Open ``lane`` for writing, on a connection that identifies its far end.

    The handshake is the safety check, and it belongs to *this attempt*: the
    greeting names the REPL on the other end of the socket and it is compared
    against the session we were told to act for. That matters more here than
    anywhere else in the service, because the daemon hands out pre-warmed
    ``spare/*.pty.sock`` paths that accept connections and greet healthily
    *before* any job claims them. An address held across a claim does not write
    into a void — it writes into somebody else's live conversation. Re-opening
    per attempt, rather than reusing one connection across retries, is what keeps
    a retry from being a second chance to type into a stranger.
    """
    conn = _connect(lane, opener=opener, sleep=sleep)
    try:
        yield conn
    finally:
        conn.close()
