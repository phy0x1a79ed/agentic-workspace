"""Put the calling session back into bypass-permissions mode.

Approving a plan does not restore the mode a session was launched with — it
restores whatever mode the session was in immediately *before* it entered plan
mode, falling back to the default. Approving from a phone arrives with that
pre-plan mode set to auto, so the session lands in auto: a classifier gates every
action, and the first thing that happens after approval is friction.

No hook can fix this from outside. Claude Code's hook contract exposes per-call
allow/deny decisions and permission *rules*, but the session's mode is internal
state with no external setter. The only lever is the one a human has — the
Shift+Tab permission-mode cycle — which is exactly the kind of thing reflection
exists to press on a session's own behalf.

The cycle is ``default → acceptEdits → plan → bypassPermissions → auto →
default``, and ``bypassPermissions``/``auto`` only appear in it when available in
that session. Counting presses is therefore not safe: the count depends on where
you start, an unavailable mode silently shortens the cycle, and overshooting
parks the session in *plan mode*, which is worse than where it started. So this
never counts. It reads the mode the TUI is displaying, presses once, and reads
again — and if bypass never shows up it walks the session back to where it began
and says so.

Two walks on one session must never overlap. Each reads the footer the other's
press just changed, so between them they step past bypass and each concludes the
cycle never offered it — observed live, when a plan hook's HTTP timeout fired
mid-walk and its retry started a second walk while the first was still pressing.
Walks are serialised per session, and a walk that waited its turn starts from a
fresh read, which is usually "already in bypass".
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from awm import claudedaemon
from awm.reflection import guards, session_target, tmux_inject

log = logging.getLogger("awm.reflection.permission_mode")

# How the TUI footer names each permission mode. These are TUI copy and can move
# under a CLI update. They are now the *only* screen scraping left in the service —
# the completion watcher used to read the pane for "esc to interrupt" and
# "Compacting conversation" and now reads the session's own status field instead —
# and they survive here because there is no structured equivalent to read: the mode
# is internal state with no external setter and no record field. They at least fail
# safe: an unrecognised footer reads as "unknown", which refuses rather than
# pressing keys blindly.
#
# `default` draws no indicator before Claude Code 2.1.310 and "manual mode on"
# from it. A session started in plan mode returns to `default` on approval, so a
# walk that cannot see `default` refuses exactly the sessions it exists for.
_MODE_MARKERS: dict[str, tuple[str, ...]] = {
    "default": ("manual mode on",),
    "bypassPermissions": ("bypass permissions on",),
    "acceptEdits": ("accept edits on",),
    "plan": ("plan mode on",),
    "auto": ("auto mode on",),
}
TARGET_MODE = "bypassPermissions"

# The cycle visits at most five modes, so a full lap plus one is a generous cap.
# Its only job is to stop a session that can't reach bypass from being cycled
# forever.
_MAX_STEPS = 6

# After a press, the footer is read back until it shows a different mode. Every
# step of the cycle changes the indicator (default's is the absence of one), so
# no change within the budget means the press did not land.
_REDRAW_POLLS = 10
_REDRAW_POLL_S = 0.1

# How long a walk waits for another walk on the same session to finish.
_WALK_WAIT_S = 10.0
_walk_locks: dict[str, threading.Lock] = {}
_walk_locks_guard = threading.Lock()

# Shift+Tab. tmux names it `BTab`; on a raw pty it is the CSI Z ("cursor back
# tab") sequence.
_TMUX_SHIFT_TAB = "BTab"
_RAW_SHIFT_TAB = b"\x1b[Z"

# The other keys a verb may press, by the name it asks for. Escape alone is the
# interrupt key; Down and Enter walk a modal's list.
_TMUX_KEYS = {"Escape": "Escape", "Enter": "Enter", "Down": "Down", "Up": "Up"}
_RAW_KEYS = {"Escape": b"\x1b", "Enter": b"\r", "Down": b"\x1b[B",
             "Up": b"\x1b[A"}

# How much trailing output to classify. The footer is the last thing drawn, and
# on the daemon path the stream still has escape sequences in it, so a window is
# both cheaper and less likely to match stale copy further up.
_TAIL_CHARS = 4000


def classify(screen: str) -> str:
    """Name the permission mode a screen is showing, or ``"unknown"``.

    The *last* marker in the window wins, not the first one found. This matters:
    both capture paths hand back text that still contains earlier paints of the
    footer, so after a mode change the old label and the new one are both present
    and only their order says which is current.

    Only what lies under the last prompt box counts, where the footer is. Text
    above it is transcript, and a plan-mode start opens with the notice "Session
    was started with bypass permissions, so plan mode won't ask…", which read as
    bypass. No prompt box means a dialog is drawn in its place, which reads as
    unknown.

    An older CLI draws no indicator for ``"default"``, which is indistinguishable
    from a footer we failed to capture, so there it reads as unknown. That is the
    honest answer, and callers treat unknown as "do not act".

    Pass a rendered screen (see :func:`_footer`), not a background session's raw
    pty stream. A busy session's spinner repaints push the footer out of the
    tail window of a raw stream, so bypass read as unknown and the walk pressed
    straight past it.
    """
    footer = guards.under_prompt_box(screen)
    if footer is None:
        return "unknown"
    tail = footer[-_TAIL_CHARS:].lower()
    best_mode, best_at = "unknown", -1
    for mode, markers in _MODE_MARKERS.items():
        at = max(tail.rfind(m) for m in markers)
        if at > best_at:
            best_mode, best_at = mode, at
    return best_mode


class _TmuxSession:
    """Read the footer and press Shift+Tab in a tmux pane."""

    def __init__(self, target: session_target.TmuxLane, *,
                 socket: Optional[str] = None, runner=None) -> None:
        self._target = target
        self._socket = socket
        self._kw = {"socket": socket}
        if runner is not None:
            self._kw["runner"] = runner

    @property
    def label(self) -> str:
        return self._target.name or self._target.pane

    def read(self) -> str:
        return tmux_inject.capture_pane(self._target.pane, **self._kw)

    def glance(self) -> str:
        return self.read()

    def repaint(self) -> bool:
        # `capture-pane` renders the screen as it stands; nothing is missing.
        return False

    def forget(self) -> None:
        # `capture-pane` renders the *current* screen, so there is no stale copy
        # to discard here — see the daemon session for why this exists at all.
        pass

    def cycle(self) -> None:
        runner = self._kw.get("runner", tmux_inject.subprocess.run)
        tmux_inject._run(
            tmux_inject._base_argv(self._socket)
            + ["send-keys", "-t", self._target.pane, _TMUX_SHIFT_TAB],
            runner)

    def press(self, key: str) -> None:
        runner = self._kw.get("runner", tmux_inject.subprocess.run)
        tmux_inject._run(
            tmux_inject._base_argv(self._socket)
            + ["send-keys", "-t", self._target.pane, _TMUX_KEYS[key]], runner)

    def close(self) -> None:
        pass


class _DaemonSession:
    """Read the footer and press Shift+Tab over a background session's PTY."""

    def __init__(self, target: session_target.DaemonLane, *,
                 opener=claudedaemon.open_unix,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._target = target
        self._conn = claudedaemon.connect(target, opener=opener, sleep=sleep)

    @property
    def label(self) -> str:
        return self._target.name or self._target.session_id

    def read(self) -> str:
        return self._conn.screen(quiet_for=0.3, cap=3.0)

    def glance(self) -> str:
        """The stream so far, after a short listen.

        ``read`` waits for the stream to go quiet, which a busy session's spinner
        never lets it do, so every read there costs the full cap. A poll does not
        need quiet, only the next paint.
        """
        return self._conn.screen(quiet_for=0.1, cap=0.3)

    def repaint(self) -> bool:
        """Have the session redraw its screen. False when its size is unknown."""
        if not self._target.geometry:
            return False
        self._conn.repaint(*self._target.geometry)
        return True

    def forget(self) -> None:
        """Discard buffered output so the next read only sees what comes next.

        The pty stream is append-only, unlike a rendered screen: every footer
        ever drawn is still in the buffer. That matters because `default` draws
        *no* mode indicator, so without this the previous mode's label stays the
        last marker in the window and a session sitting in `default` reads back
        as whatever it was before. Classifying only post-keypress output makes
        "no indicator" mean what it says.
        """
        self._conn.output.clear()

    def cycle(self) -> None:
        self._conn.send_keys(_RAW_SHIFT_TAB)

    def press(self, key: str) -> None:
        self._conn.send_keys(_RAW_KEYS[key])

    def close(self) -> None:
        self._conn.close()


def _open(target, *, socket=None, runner=None, opener=None, sleep=time.sleep):
    if isinstance(target, session_target.DaemonLane):
        kw = {"sleep": sleep}
        if opener is not None:
            kw["opener"] = opener
        return _DaemonSession(target, **kw)
    return _TmuxSession(target, socket=socket, runner=runner)


def open_session(target, **kw):
    """Open ``target`` to read its screen and press keys; close it when done."""
    return _open(target, **kw)


def _footer(sess) -> str:
    """The mode the session's screen shows now."""
    return classify(guards.current_screen(sess.glance()))


def _await_change(sess, before: str, sleep: Callable[[float], None]) -> str:
    now = before
    for _ in range(_REDRAW_POLLS):
        sleep(_REDRAW_POLL_S)
        now = _footer(sess)
        if now != before:
            break
    return now


def _walk_lock(target) -> threading.Lock:
    key = getattr(target, "session_id", None) or repr(target)
    with _walk_locks_guard:
        return _walk_locks.setdefault(key, threading.Lock())


def ensure_bypass(*, caller_pid: Optional[int], socket: Optional[str] = None,
                  runner=None, opener=None,
                  sleep: Callable[[float], None] = time.sleep,
                  expect_session: Optional[str] = None) -> dict:
    """Cycle the calling session's permission mode until it reads as bypass.

    Returns a result dict describing what happened: ``changed`` says whether any
    key was pressed, ``mode`` is where the session ended up, and ``steps`` is how
    many times Shift+Tab was sent.

    ``expect_session`` narrows which session the pid is allowed to resolve to —
    see :func:`session_target.resolve`. It refuses on a mismatch and never
    redirects.
    """
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is no permission "
            "mode to change. Reflection acts on the caller's own session only")
    target = session_target.resolve(caller_pid, socket=socket, runner=runner,
                                    expect_session=expect_session)
    lock = _walk_lock(target)
    if not lock.acquire(timeout=_WALK_WAIT_S):
        return {"ok": False, "changed": False, "mode": "unknown", "steps": 0,
                "session": target.name or target.session_id,
                "hosting": target.hosting,
                "error": ("another permission-mode change on this session is "
                          "still running. Nothing was sent.")}
    try:
        return _walk(target, socket=socket, runner=runner, opener=opener,
                     sleep=sleep)
    finally:
        lock.release()


def _walk(target, *, socket, runner, opener, sleep) -> dict:
    sess = _open(target, socket=socket, runner=runner, opener=opener, sleep=sleep)
    try:
        started_as = classify(guards.current_screen(sess.read()))
        # A session busy for a while painted its footer before anything the
        # daemon replays to us, so ask it to draw the screen again.
        if started_as == "unknown" and sess.repaint():
            started_as = classify(guards.current_screen(sess.read()))
        if started_as == TARGET_MODE:
            return {"ok": True, "changed": False, "mode": TARGET_MODE, "steps": 0,
                    "session": sess.label, "hosting": target.hosting,
                    "detail": "already in bypass-permissions mode"}
        if started_as == "unknown":
            # No mode indicator on screen means the footer is covered — almost
            # always by a modal (a picker, a permission prompt). Those swallow
            # keystrokes and navigate on them, which is exactly what the
            # INTERACTIVE guard refuses to walk into elsewhere. Pressing blind
            # here could select an arbitrary menu entry, so don't.
            return {
                "ok": False, "changed": False, "mode": "unknown", "steps": 0,
                "session": sess.label, "hosting": target.hosting,
                "error": (
                    "cannot see this session's permission-mode indicator, so the "
                    "mode cannot be changed safely — on a terminal session that "
                    "means a modal or prompt is covering the footer and would "
                    "swallow the keystroke; on a background session it usually "
                    "means the pty stream carries no footer paint to read. "
                    "Nothing was sent."),
            }

        seen = [started_as]
        for step in range(1, _MAX_STEPS + 1):
            sess.cycle()
            now = _await_change(sess, seen[-1], sleep)
            seen.append(now)
            if now == TARGET_MODE:
                log.info("reflection: %s → bypass permissions in %d step(s) (%s)",
                         sess.label, step, " → ".join(seen))
                return {"ok": True, "changed": True, "mode": TARGET_MODE,
                        "steps": step, "session": sess.label,
                        "hosting": target.hosting,
                        "started_as": started_as, "path": seen}
            # Back where we started without passing through bypass: this session
            # does not offer it (a remote-attached session, or one launched
            # without permission to bypass). Stop here — we are already home.
            if step > 1 and now == started_as:
                break

        log.warning("reflection: %s never offered bypass permissions (saw %s)",
                    sess.label, " → ".join(seen))
        return {
            "ok": False,
            "changed": True,
            "mode": seen[-1],
            "steps": len(seen) - 1,
            "session": sess.label,
            "hosting": target.hosting,
            "started_as": started_as,
            "path": seen,
            "error": (
                "this session's permission-mode cycle never offers bypass "
                "permissions, so it cannot be switched into it. That is usually a "
                "session that was not started with --dangerously-skip-permissions, "
                "or one where bypass is disabled. Nothing else was changed."),
        }
    finally:
        sess.close()
