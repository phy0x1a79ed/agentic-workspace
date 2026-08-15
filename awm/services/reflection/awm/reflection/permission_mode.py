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
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from awm.reflection import daemon_inject, session_target, tmux_inject

log = logging.getLogger("awm.reflection.permission_mode")

# How the TUI footer names each permission mode. These are TUI copy and can move
# under a CLI update. They are now the *only* screen scraping left in the service —
# the completion watcher used to read the pane for "esc to interrupt" and
# "Compacting conversation" and now reads the session's own status field instead —
# and they survive here because there is no structured equivalent to read: the mode
# is internal state with no external setter and no record field. They at least fail
# safe: an unrecognised footer reads as "unknown", which refuses rather than
# pressing keys blindly.
_MODE_MARKERS: dict[str, tuple[str, ...]] = {
    "bypassPermissions": ("bypass permissions",),
    "acceptEdits": ("accept edits on",),
    "plan": ("plan mode on",),
    "auto": ("auto mode on",),
}
TARGET_MODE = "bypassPermissions"

# The cycle visits at most five modes, so a full lap plus one is a generous cap.
# Its only job is to stop a session that can't reach bypass from being cycled
# forever.
_MAX_STEPS = 6

# Beat between pressing Shift+Tab and reading the footer back, so we classify the
# redrawn footer rather than the one we just invalidated.
_REDRAW_S = 0.35

# Shift+Tab. tmux names it `BTab`; on a raw pty it is the CSI Z ("cursor back
# tab") sequence.
_TMUX_SHIFT_TAB = "BTab"
_RAW_SHIFT_TAB = b"\x1b[Z"

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

    ``"default"`` is not detectable — it is the absence of an indicator, which is
    indistinguishable from a footer we failed to capture — so it reads as
    unknown. That is the honest answer, and callers treat unknown as "do not act".

    Known gap, background sessions only: that backend reads an append-only byte
    stream rather than a rendered screen, so a session sitting in ``default``
    (which paints no indicator at all) can read back as whatever mode last
    painted one. Discarding buffered output before each keypress fixes this for
    every read *during* a walk; the very first read has nothing to discard and
    can still be stale. The consequence is bounded — such a session may be left
    in ``default`` instead of being switched — which is no worse than the
    behaviour this verb exists to improve on. Nothing repaints the footer on
    demand (an empty bracketed paste and a cursor key were both tried), so
    closing the gap properly means rendering the stream through a terminal
    emulator, which is a dependency this service does not otherwise need. The
    tmux backend is unaffected: ``capture-pane`` renders current state.
    """
    tail = screen[-_TAIL_CHARS:].lower()
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

    def close(self) -> None:
        pass


class _DaemonSession:
    """Read the footer and press Shift+Tab over a background session's PTY."""

    def __init__(self, target: session_target.DaemonLane, *,
                 opener=daemon_inject._open_unix,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._target = target
        self._conn = daemon_inject._connect(target, opener=opener, sleep=sleep)

    @property
    def label(self) -> str:
        return self._target.name or self._target.session_id

    def read(self) -> str:
        return self._conn.screen(quiet_for=0.3, cap=3.0)

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

    def close(self) -> None:
        self._conn.close()


def _open(target, *, socket=None, runner=None, opener=None, sleep=time.sleep):
    if isinstance(target, session_target.DaemonLane):
        kw = {"sleep": sleep}
        if opener is not None:
            kw["opener"] = opener
        return _DaemonSession(target, **kw)
    return _TmuxSession(target, socket=socket, runner=runner)


def ensure_bypass(*, caller_pid: Optional[int], socket: Optional[str] = None,
                  runner=None, opener=None,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Cycle the calling session's permission mode until it reads as bypass.

    Returns a result dict describing what happened: ``changed`` says whether any
    key was pressed, ``mode`` is where the session ended up, and ``steps`` is how
    many times Shift+Tab was sent.
    """
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is no permission "
            "mode to change. Reflection acts on the caller's own session only")
    target = session_target.resolve(caller_pid, socket=socket, runner=runner)
    sess = _open(target, socket=socket, runner=runner, opener=opener, sleep=sleep)
    try:
        started_as = classify(sess.read())
        if started_as == TARGET_MODE:
            return {"ok": True, "changed": False, "mode": TARGET_MODE, "steps": 0,
                    "session": sess.label,
                    "detail": "already in bypass-permissions mode"}
        if started_as == "unknown":
            # No mode indicator on screen means the footer is covered — almost
            # always by a modal (a picker, a permission prompt). Those swallow
            # keystrokes and navigate on them, which is exactly what the
            # INTERACTIVE guard refuses to walk into elsewhere. Pressing blind
            # here could select an arbitrary menu entry, so don't.
            return {
                "ok": False, "changed": False, "mode": "unknown", "steps": 0,
                "session": sess.label,
                "error": (
                    "cannot see this session's permission-mode indicator, so the "
                    "mode cannot be changed safely — the session is most likely "
                    "showing a modal or prompt that would swallow the keystroke. "
                    "Nothing was sent."),
            }

        seen = [started_as]
        for step in range(1, _MAX_STEPS + 1):
            sess.forget()
            sess.cycle()
            sleep(_REDRAW_S)
            now = classify(sess.read())
            seen.append(now)
            if now == TARGET_MODE:
                log.info("reflection: %s → bypass permissions in %d step(s) (%s)",
                         sess.label, step, " → ".join(seen))
                return {"ok": True, "changed": True, "mode": TARGET_MODE,
                        "steps": step, "session": sess.label,
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
