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
the calling session's and did not reject them. It can NOT confirm that the
command ran: a slash command runs at end of turn, and the reflection call has to
*return* for the caller's turn to end. At the moment of the return there is by
construction nothing to observe yet. That is the whole reason the follow-up is
deferred to a watcher instead of awaited here, and anything that later looks like
a way to verify delivery inline is this constraint being forgotten.
"""
from __future__ import annotations

import json
import logging
import socket as _socket
import struct
import threading
import time
from typing import Callable, Optional

from awm.reflection import pending, session_target
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

# How long to keep listening after the last write before closing the socket. Long
# enough for the host to say it threw the input away, short enough that it does
# not show up as latency on a verb the caller is blocking on. It is bounded by the
# cap rather than by the quiet period: a live session is streaming its own output
# the whole time, so "quiet" is not something this window can rely on reaching.
_ACCEPT_CHECK_S = 0.5
_ACCEPT_QUIET_S = 0.15

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
    """The daemon PTY socket could not be reached, or spoke an unexpected shape.

    ``wrote_input`` says whether a raw frame had already made it onto the wire
    when this went wrong. It gates the retry above: re-pasting is only safe when
    nothing landed, because ``send`` carries arbitrary text and running it twice
    is worse than not running it at all. A rejected frame counts as *not*
    written — being rejected is precisely the host saying it threw the bytes
    away — so the common case still retries.
    """

    def __init__(self, *args, wrote_input: bool = False) -> None:
        super().__init__(*args)
        self.wrote_input = wrote_input


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
            raise DaemonError(f"writing to the PTY socket failed: {exc}",
                              wrote_input=self._raw_sent) from None
        if kind == _RAW:
            self._raw_sent = True

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
        conn.check_not_rejected()
        return bool(enter)


def _deliver(text: str, target: session_target.DaemonTarget, *,
             enter: bool, opener: Opener,
             sleep: Callable[[float], None] = time.sleep,
             resolve=None) -> tuple[bool, session_target.DaemonTarget]:
    """Write ``text`` into the session, crossing one re-host on the way if need be.

    Returns ``(submitted, target)`` — the target being whichever one the write
    actually went to, which the caller needs for the pending record and the
    watcher it is about to spawn.

    The deferred-resume watcher has always re-read the roster before delivering,
    because a session that respawned mid-wait has a new socket and a new token
    and the ones handed over earlier are dead. The *initial* injection had no
    such step: it used whatever the roster said when the verb was dispatched. For
    a session being attached to and detached from a terminal — which is exactly
    what re-homes its PTY — that can already be one event out of date by the time
    the bytes are written, and the write then lands in a host nobody is reading.

    So: one re-resolve, one retry, then give up. Re-resolving by pid rather than
    by session id is what makes it safe — a session id is not stable (a ``/clear``
    mints a new one), so re-joining on it would find nothing. And one attempt, not
    a loop: the point is to cross a single re-host, not to grind against a session
    that is genuinely unreachable.

    This is only sound on top of the greeting check in :meth:`Connection.handshake`.
    Without it, a retry against a re-resolved socket is just as capable of typing
    into a stranger — twice.
    """
    resolve = resolve or session_target.resolve
    try:
        return _paste_and_submit(text, target, enter=enter, opener=opener,
                                 sleep=sleep), target
    except DaemonError as first:
        if getattr(first, "wrote_input", False):
            # Something already went onto the wire, so we cannot know whether it
            # landed. `send` carries arbitrary text; running it twice is a worse
            # outcome than not running it at all.
            log.warning("reflection: reaching background session %s failed after "
                        "input was already written (%s); not retrying, because a "
                        "second attempt could deliver it twice",
                        target.name or target.session_id, first)
            raise
        log.warning("reflection: first attempt to reach background session %s "
                    "failed (%s); re-reading the roster and retrying once",
                    target.name or target.session_id, first)
        try:
            fresh = resolve(target.repl_pid)
        except session_target.ResolveError as exc:
            raise DaemonError(
                f"{first}; and the session could not be re-resolved to retry: "
                f"{exc}") from None
        if not isinstance(fresh, session_target.DaemonTarget):
            raise DaemonError(
                f"{first}; and on re-resolving, pid {target.repl_pid} is no "
                f"longer a background session — reflection will not switch "
                f"transports mid-call") from None
        return _paste_and_submit(text, fresh, enter=enter, opener=opener,
                                 sleep=sleep), fresh


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
            pending.clear(target.repl_pid)
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
    # Forget the promise BEFORE delivering on it, not after. The two orderings
    # trade opposite failure modes across a restart in the gap: clearing after
    # would let a boot-time replay re-deliver a resume that already landed,
    # injecting a stray prompt into a session that is busy working — whereas
    # clearing first can only lose a resume if the process dies inside the
    # sub-second paste, and that is the same hole every other step has.
    pending.clear(target.repl_pid)
    # Re-read the roster: a background session that respawned mid-wait has a new
    # PTY socket and a new input token, and the ones we were handed at injection
    # time are dead. Re-resolving by pid is what makes this safe: the session id
    # may well have changed under us — a /clear mints a new one — so re-joining
    # on it would find nothing.
    try:
        fresh = session_target.resolve(target.repl_pid)
    except session_target.ResolveError:
        fresh = target
    if not isinstance(fresh, session_target.DaemonTarget):
        fresh = target
    try:
        _deliver(followup, fresh, enter=True, opener=opener, sleep=sleep)
    except DaemonError as exc:
        log.warning("reflection: could not inject resume into background "
                    "session %s: %s", target.name or target.session_id, exc)


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="reflection-followup-bg", daemon=True).start()


def resume_watch(item: pending.Pending, target: session_target.DaemonTarget, *,
                 opener: Opener = _open_unix,
                 spawn: Callable[[Callable[[], None]], object] = _default_spawn
                 ) -> None:
    """Pick a promised follow-up back up after a service restart.

    Only the *waiting* half is replayed — the command itself was injected before
    the restart and must not be sent again. Carrying the original
    ``injected_at_ms`` through is what keeps the "has it reacted yet?" test
    meaningful: the session's status timestamp is compared against when the
    command actually went in, not against when this process happened to boot.
    """
    spawn(lambda: _await_and_followup(item.text, item.followup, target,
                                      opener=opener,
                                      injected_at_ms=item.injected_at_ms))


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
    # A failure here propagates: the caller is told the command did not go in,
    # and — because this sits *above* the record-and-spawn below — no promise is
    # left behind for a resume to a command that was never injected.
    submitted, target = _deliver(text, target, enter=enter, opener=opener,
                                 sleep=sleep)

    followup_sent = None
    followup_deferred = False
    if submitted and is_slash(text):
        followup_sent = resume_text(followup)
        followup_deferred = True
        # Written down before the watcher starts: the watcher is a thread in this
        # process, and the gateway restarts this process out from under it.
        pending.record(pending.Pending(
            repl_pid=target.repl_pid,
            proc_start=session_target._proc_start(target.repl_pid) or "",
            session_id=target.session_id, text=text, followup=followup_sent,
            injected_at_ms=injected_at_ms, name=target.name, hosting="background"))
        spawn(lambda: _await_and_followup(text, followup_sent, target,
                                          opener=opener,
                                          injected_at_ms=injected_at_ms,
                                          sleep=sleep))

    return {"ok": True, "session": target.name or target.session_id,
            "hosting": "background", "text": text,
            "submitted": submitted, "followup": followup_sent,
            "followup_deferred": followup_deferred}
