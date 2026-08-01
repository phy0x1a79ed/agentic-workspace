"""Inject a command line into an arbitrary tmux pane and (optionally) submit it.

This is the whole substance of the reflection service. An agent running the
interactive ``claude`` (or ``opencode``) TUI inside a tmux pane cannot type a
control command into itself — but any process on the host, as the same user, can
paste one into that pane and press Enter. tmux buffers the input and the TUI runs
it the instant the current turn ends, which is exactly when a self-directed
``/compact`` should fire.

The paste sequence mirrors ``awm.agentcore.claude_backend._paste_prompt``:
``load-buffer`` (stdin) → ``paste-buffer -d -p`` (``-p`` = *bracketed* paste, so a
leading ``/`` lands as literal text instead of opening the TUI's slash menu) →
``send-keys Enter``. Crucially it sends **no Escape** — Escape would cancel the
agent's in-flight turn (that is ``claude_backend.interrupt``'s job); we want the
command to *queue* behind the current turn, not interrupt it.

The service is a separate gateway-spawned process, so it does NOT share the
caller's environment — the target pane arrives as an argument (the caller's
``$TMUX_PANE``). Best-effort auto-detection is provided for the single-agent case.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("awm.reflection.tmux_inject")

# Slash commands that irreversibly discard context / end the session. Refused
# unless the caller passes confirm=true.
DESTRUCTIVE = {"/clear", "/quit", "/exit"}

# Slash commands that open a blocking modal / picker in the TUI (a settings pane,
# a navigable list, …). Pasted input and Enter are SWALLOWED by the modal — a
# follow-up prompt does not escape it, and for a navigable list an Enter drills
# deeper — so reflection cannot drive them and would leave the session frozen
# (only a hand-typed Escape recovers). Refused outright; run these by hand.
# Empirically verified for /status and /mcp (both trap input; Esc-only exit).
# Best-effort curated list — extend as new modal commands appear.
INTERACTIVE = {
    "/mcp", "/status", "/config", "/permissions", "/agents",
    "/hooks", "/resume", "/theme", "/login", "/logout",
    "/bashes", "/doctor", "/statusline", "/vim",
}

# Commands that act directly WITH an argument but open a picker modal when bare
# (e.g. `/model opus` switches immediately; `/model` alone opens the chooser).
_MODAL_WHEN_BARE = {"/model"}

# Settle beat between the paste landing and the Enter that submits it, so the
# TUI has focused the pasted input before we press return.
_SETTLE_S = 0.15

# --- Deferred follow-up (the fix for resume-before-compact) ---------------
# A slash command and its resume prompt must NEVER be co-queued: `/compact` is a
# slash command that only runs when the session goes idle, whereas the resume is
# a regular message an *active* agent consumes immediately — so co-queuing lets
# the agent run the resume first (on the old context) and starves `/compact` of
# its idle slot. Instead we inject the command alone and hand the resume to a
# detached watcher that polls the pane and injects it only once the command has
# visibly completed (busy → idle, and for /compact the `Compacted` marker).
_POLL_S = 2.0            # capture-pane poll cadence while awaiting completion
_IDLE_CONFIRM_POLLS = 3  # consecutive idle samples that count as "settled" (a
                         # no-op /compact, or any command done without a marker) —
                         # a transient idle beat before compaction starts won't
                         # reach this, so the resume can't fire in that gap
_FOLLOWUP_MAX_WAIT_S = 900.0   # hard cap; inject anyway past this (resume beats hang)
_FAST_POLL_S = 0.3       # poll cadence before the driving turn/compaction is
                         # observed to have started, so a brief compacting
                         # window (or an agent that self-resumes the instant
                         # compaction ends) isn't skipped between samples

# TUI pane-state markers (calibrated against the interactive `claude` TUI). Order
# of checks matters: an active turn shows the `esc to interrupt` footer even when
# a *previous* compaction's `Compacted` line still lingers in scrollback, so busy
# is classified before compacted to avoid firing on a stale marker.
_COMPACTING_MARKERS = ("Compacting conversation",)
_BUSY_MARKERS = ("esc to interrupt",)
_COMPACTED_MARKERS = ("Compacted",)   # "Compacted (ctrl+o to see full summary)"

# A bare slash command (e.g. /compact) runs at end-of-turn and then leaves the
# session idle with nothing to do — for an autonomous agent that is death. So a
# self-directed slash command must be trailed by a real prompt that gives the
# session a next turn once the command completes. This is the default when the
# caller does not supply their own `followup`.
DEFAULT_FOLLOWUP = "Continue with what you were doing."


def is_slash(text: str) -> bool:
    """True if ``text`` is a slash command (a TUI control command, not a prompt)."""
    return text.strip().startswith("/")


def opens_modal(text: str) -> bool:
    """True if ``text`` opens a blocking modal/picker that traps pasted input.

    Covers the curated :data:`INTERACTIVE` set plus arg-less forms in
    :data:`_MODAL_WHEN_BARE` (``/model`` alone opens a chooser; ``/model opus``
    acts directly).
    """
    parts = text.strip().split()
    first = parts[0]
    if first in INTERACTIVE:
        return True
    if first in _MODAL_WHEN_BARE and len(parts) == 1:
        return True
    return False

Runner = Callable[..., subprocess.CompletedProcess]


class TmuxError(RuntimeError):
    """A tmux invocation failed or the target pane could not be resolved."""


def _tmux_bin() -> str:
    # shutil.which honors PATH; fall back to the conventional absolute path for
    # systemd's minimal PATH where `which` may still resolve but be defensive.
    import shutil
    return shutil.which("tmux") or "/usr/bin/tmux"


def _base_argv(socket: Optional[str]) -> list[str]:
    argv = [_tmux_bin()]
    if socket:
        argv += ["-S", socket]
    return argv


def _run(argv: list[str], runner: Runner, **kw: Any) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    proc = runner(argv, **kw)
    if proc.returncode != 0:
        err = proc.stderr
        if isinstance(err, (bytes, bytearray)):
            err = err.decode("utf-8", "replace")
        raise TmuxError(f"{' '.join(argv[1:3])}… failed: {(err or '').strip()}")
    return proc


# ---------------------------------------------------------------------------
# Pane discovery (best-effort — explicit `pane` is the norm)
# ---------------------------------------------------------------------------

def list_panes(*, socket: Optional[str] = None,
               runner: Runner = subprocess.run) -> list[dict]:
    """List every pane on the tmux server as ``{pane, pid, command, session, activity}``."""
    fmt = ("#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}"
           "\t#{pane_activity}")
    proc = _run(_base_argv(socket) + ["list-panes", "-a", "-F", fmt],
                runner, text=True)
    panes: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            activity = int(parts[4]) if len(parts) > 4 else 0
        except ValueError:
            activity = 0
        panes.append({"pane": parts[0], "pid": parts[1],
                      "command": parts[2], "session": parts[3],
                      "activity": activity})
    return panes


def _ppid_children() -> dict[int, list[int]]:
    """Map ppid → [child pids] by scanning /proc (Linux)."""
    kids: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fp:
                data = fp.read()
            # comm (field 2) is parenthesized and may contain spaces; ppid is
            # the field right after the closing paren.
            fields = data[data.rfind(")") + 2:].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fp:
            return fp.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _subtree_has_agent(root_pid: int, kids: dict[int, list[int]]) -> bool:
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        cl = _cmdline(pid).lower()
        if "claude" in cl or "opencode" in cl:
            return True
        stack.extend(kids.get(pid, []))
    return False


def autodetect_pane(*, socket: Optional[str] = None,
                    runner: Runner = subprocess.run) -> str:
    """Return a tmux pane running an agent.

    Multiple candidate panes are not an error — an agent can plausibly share
    a tmux server with other agent panes, and blocking the caller over that
    ambiguity is worse than picking one. The most-recently-active candidate
    (by tmux's own ``pane_activity`` timestamp) is the best guess for "the
    pane that just made this call", so it wins; the full candidate list is
    still logged for visibility.
    """
    panes = list_panes(socket=socket, runner=runner)
    kids = _ppid_children()
    matches = [p for p in panes
               if p["pid"].isdigit() and _subtree_has_agent(int(p["pid"]), kids)]
    if not matches:
        raise TmuxError(
            "could not auto-detect an agent pane; pass your $TMUX_PANE explicitly")
    if len(matches) == 1:
        return matches[0]["pane"]
    matches.sort(key=lambda p: p["activity"], reverse=True)
    cand = ", ".join(f'{p["pane"]} ({p["session"]})' for p in matches)
    log.info("reflection: multiple agent panes found (%s); picking most "
             "recently active: %s", cand, matches[0]["pane"])
    return matches[0]["pane"]


def _assert_pane_exists(pane: str, socket: Optional[str], runner: Runner) -> None:
    _run(_base_argv(socket) + ["display-message", "-p", "-t", pane, "#{pane_id}"],
         runner, text=True)


def _assert_pane_has_agent(pane: str, socket: Optional[str], runner: Runner) -> None:
    """Refuse to inject into a pane whose process subtree has no agent.

    Guards every path that can produce a `pane` value (explicit arg,
    header-derived default, auto-detect) against the one case none of them
    rules out: the pane id is real and exists, but nothing running there is a
    `claude`/`opencode` process (a stale id repointed at a shell/editor, or an
    outright wrong explicit `pane` argument). Reuses the same subtree walk
    `autodetect_pane` already pays for.
    """
    proc = _run(_base_argv(socket) + ["display-message", "-p", "-t", pane,
                                       "#{pane_pid}"], runner, text=True)
    pid_s = (proc.stdout or "").strip()
    if not pid_s.isdigit() or not _subtree_has_agent(int(pid_s), _ppid_children()):
        raise TmuxError(
            f"pane {pane} is not running an agent (claude/opencode); refusing "
            f"to inject — pass the correct pane or leave it unset for auto-detect")


# ---------------------------------------------------------------------------
# The one primitive
# ---------------------------------------------------------------------------

def _paste_and_submit(text: str, pane: str, *, enter: bool,
                      socket: Optional[str], runner: Runner) -> bool:
    """Paste ``text`` into ``pane`` (bracketed) and press Enter when ``enter``.

    Returns whether an Enter was sent. No Escape is ever sent, so an in-flight
    turn is queued rather than interrupted.
    """
    buf = "awm-rfl-" + secrets.token_hex(4)
    _run(_base_argv(socket) + ["load-buffer", "-b", buf, "-"],
         runner, input=text.encode("utf-8"), capture_output=True)
    _run(_base_argv(socket) + ["paste-buffer", "-d", "-p", "-b", buf, "-t", pane],
         runner)
    if enter:
        time.sleep(_SETTLE_S)
        _run(_base_argv(socket) + ["send-keys", "-t", pane, "Enter"], runner)
        return True
    return False


# ---------------------------------------------------------------------------
# Deferred follow-up: watch the pane, inject the resume only after the command
# has visibly finished (never co-queued with it).
# ---------------------------------------------------------------------------

def capture_pane(pane: str, *, socket: Optional[str] = None,
                 runner: Runner = subprocess.run) -> str:
    """Return the visible text of ``pane`` (``tmux capture-pane -p``)."""
    proc = _run(_base_argv(socket) + ["capture-pane", "-t", pane, "-p"],
                runner, text=True)
    return proc.stdout or ""


def pane_session(pane: str, *, socket: Optional[str] = None,
                 runner: Runner = subprocess.run) -> Optional[str]:
    """Return ``pane``'s tmux session name, or ``None`` if it can't be read.

    Captured alongside the pane at injection time so the deferred follow-up
    watcher can recover if the specific pane id is destroyed mid-wait (the
    session name is the more stable identity across a within-session pane
    reshuffle — e.g. the agent was backgrounded and reattached elsewhere).
    """
    try:
        proc = _run(_base_argv(socket) + ["display-message", "-p", "-t", pane,
                                          "#{session_name}"], runner, text=True)
    except TmuxError:
        return None
    name = (proc.stdout or "").strip()
    return name or None


def _reresolve_pane(session: str, *, socket: Optional[str],
                    runner: Runner) -> Optional[str]:
    """Return ``session``'s current pane id, or ``None`` if the session is gone."""
    try:
        proc = _run(_base_argv(socket) + ["display-message", "-p", "-t", session,
                                          "#{pane_id}"], runner, text=True)
    except TmuxError:
        return None
    pane = (proc.stdout or "").strip()
    return pane or None


def _pane_phase(snapshot: str) -> str:
    """Classify the pane tail as ``compacting`` / ``busy`` / ``compacted`` / ``idle``.

    Checked in that order deliberately: a running turn shows the ``esc to
    interrupt`` footer, and a *stale* ``Compacted`` line from an earlier compaction
    can still sit in scrollback — so an active turn must classify as busy, not
    compacted, or the watcher would fire on the old marker.
    """
    tail = "\n".join(snapshot.splitlines()[-40:])
    if any(m in tail for m in _COMPACTING_MARKERS):
        return "compacting"
    if any(m in tail for m in _BUSY_MARKERS):
        return "busy"
    if any(m in tail for m in _COMPACTED_MARKERS):
        return "compacted"
    return "idle"


def _await_and_followup(text: str, followup: str, pane: str, *,
                        socket: Optional[str], runner: Runner,
                        session: Optional[str] = None,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> None:
    """Block until the injected command has finished, then submit ``followup``.

    Runs on a detached thread (a synchronous wait would deadlock — the agent's
    turn would never end, so the queued ``/compact`` would never run).

    The fire condition is edge-based, not state-based: once a ``compacting``
    sample has been observed, the very *next* sample — whatever phase it reads,
    busy, compacted, or idle — means the command has visibly finished, because
    reaching ``compacting`` at all already rules out a stale marker or a
    transient pre-compaction idle beat. This matters because an agent can
    resume its own next turn immediately once ``/compact`` completes (no idle
    gap) — waiting for a settled idle streak or the ``Compacted`` marker alone
    would miss that and only fire on the ``_FOLLOWUP_MAX_WAIT_S`` hard cap,
    arriving late and queued behind an unrelated turn. The ``compacted``-marker
    and settled-idle paths remain as a fallback for the case where compaction is
    fast enough that the ``compacting`` phase itself is never sampled.

    Polling runs at a tighter cadence (``_FAST_POLL_S``) until the driving turn
    is observed busy, to shrink the odds of a fast compacting→done transition
    landing entirely between two samples; it relaxes to ``_POLL_S`` afterward.

    If the pane vanishes mid-wait (the agent was backgrounded and its session
    ended up on a different pane — plausible with multiple agent panes in play
    at once), ``session`` (the pane's tmux session name, captured at injection
    time) is used to re-resolve the session's *current* pane and the wait
    continues against it, carrying over all state accumulated so far. Only a
    vanished *session* (not just the specific pane) ends the wait early.
    """
    is_compact = text.strip().split()[0] == "/compact"
    deadline = clock() + _FOLLOWUP_MAX_WAIT_S
    saw_busy = False
    saw_compacting = False
    idle_streak = 0
    ready = False
    while clock() < deadline:
        try:
            phase = _pane_phase(capture_pane(pane, socket=socket, runner=runner))
        except TmuxError:
            new_pane = _reresolve_pane(session, socket=socket, runner=runner) \
                if session else None
            if new_pane is None:
                log.warning("reflection: pane %s (session %s) vanished while "
                            "awaiting completion; no resume injected",
                            pane, session)
                return
            log.info("reflection: pane %s vanished, resumed watching session "
                     "%s on pane %s", pane, session, new_pane)
            pane = new_pane
            sleep(_POLL_S)
            continue

        if saw_compacting and phase != "compacting":
            # Compaction has visibly ended — fire regardless of what the pane
            # does next (settle idle, self-resume into a new turn, or show the
            # Compacted marker). Unambiguous once compacting was observed: a
            # transient pre-compaction idle beat can no longer be confused with
            # this, since compacting can only be reached after that beat.
            ready = True
            break
        if phase == "compacting":
            saw_busy = True
            saw_compacting = True
            idle_streak = 0
        elif phase == "busy":
            saw_busy = True
            idle_streak = 0
        elif saw_busy and is_compact and phase == "compacted":
            ready = True          # real compaction finished — resume at once
            break
        elif saw_busy and phase == "idle":
            # Settled idle (a no-op /compact, or a command that leaves no marker).
            # Require several consecutive samples so a brief gap before compaction
            # starts does not fire the resume early.
            idle_streak += 1
            if idle_streak >= _IDLE_CONFIRM_POLLS:
                ready = True
                break
        else:
            # Not busy yet, or a stale `Compacted` from a prior compaction while
            # saw_busy is still False — ignore and keep waiting.
            idle_streak = 0
        sleep(_POLL_S if saw_busy else _FAST_POLL_S)
    if not ready:
        log.warning("reflection: command %r completion not observed within %ss; "
                    "injecting resume anyway", text, _FOLLOWUP_MAX_WAIT_S)
    try:
        fresh = autodetect_pane(socket=socket, runner=runner)
        if fresh != pane:
            log.info("reflection: agent bounced from pane %s to %s before "
                     "resume; injecting into the current pane", pane, fresh)
        pane = fresh
    except TmuxError:
        pass  # no agent pane found right now — fall back to the cached one
    try:
        _paste_and_submit(followup, pane, enter=True, socket=socket, runner=runner)
    except TmuxError:
        log.warning("reflection: pane %s gone before resume could be injected", pane)


def _default_spawn(fn: Callable[[], None]) -> None:
    """Run ``fn`` on a detached daemon thread (the service is long-lived)."""
    threading.Thread(target=fn, name="reflection-followup", daemon=True).start()


def send(text: str, *, pane: Optional[str] = None, enter: bool = True,
         delay_ms: int = 0, confirm: bool = False,
         followup: Optional[str] = None,
         socket: Optional[str] = None,
         runner: Runner = subprocess.run,
         spawn: Callable[[Callable[[], None]], Any] = _default_spawn) -> dict:
    """Paste ``text`` into ``pane`` and (unless ``enter=False``) submit it.

    Returns a result dict. Two guards can refuse before anything is pasted
    (``{"ok": False, "refused": True, ...}``):

    * A destructive command (see :data:`DESTRUCTIVE`) with ``confirm`` false.
    * A modal/interactive command (see :data:`opens_modal`) — these trap pasted
      input and would freeze the session; refused outright, no override.

    When a *submitted* slash command is not one of the above, a follow-up prompt
    is enforced: a bare slash command (e.g. ``/compact``) runs at end-of-turn and
    then leaves the session idle with nothing to do, so a trailing prompt gives
    the session a next turn. Crucially the follow-up is **deferred** — it is NOT
    queued behind the command (that would let an active agent run the resume first,
    on the old context, and starve ``/compact`` of its idle slot). Instead a
    detached watcher (``spawn``) injects it only once the command has visibly
    completed. The call returns immediately with ``followup_deferred: True``.
    ``followup`` overrides the default text (:data:`DEFAULT_FOLLOWUP`).
    ``delay_ms`` waits before injecting; a leading ``/`` is safe thanks to
    bracketed paste.
    """
    if not text or not text.strip():
        raise ValueError("text is required")
    first = text.strip().split()[0]
    if first in DESTRUCTIVE and not confirm:
        return {
            "ok": False,
            "refused": True,
            "reason": f"{first!r} irreversibly discards context; "
                      f"pass confirm=true to proceed.",
            "guard": sorted(DESTRUCTIVE),
        }
    if opens_modal(text):
        return {
            "ok": False,
            "refused": True,
            "kind": "interactive",
            "reason": f"{first!r} opens an interactive modal/picker that "
                      f"swallows pasted input; reflection cannot navigate it and "
                      f"it would freeze the session (only a hand-typed Esc "
                      f"recovers). Run it by hand.",
            "guard": sorted(INTERACTIVE),
        }

    if pane is None:
        pane = autodetect_pane(socket=socket, runner=runner)
    _assert_pane_exists(pane, socket, runner)
    _assert_pane_has_agent(pane, socket, runner)

    if delay_ms and delay_ms > 0:
        time.sleep(min(delay_ms, 25_000) / 1000.0)

    submitted = _paste_and_submit(text, pane, enter=enter,
                                  socket=socket, runner=runner)

    # A submitted slash command needs a real prompt behind it or the session goes
    # idle/dead once the command completes. But the resume must NOT be co-queued
    # with the command (an active agent would run the resume first, on the old
    # context, deferring the command). Defer it to a detached watcher that injects
    # it only after the command has visibly finished. Plain prompts are their own
    # turn — no follow-up.
    followup_sent = None
    followup_deferred = False
    if submitted and is_slash(text):
        followup_sent = (followup.strip() if followup and followup.strip()
                         else DEFAULT_FOLLOWUP)
        followup_deferred = True
        # Captured now (not inside the watcher thread) so a pane that vanishes
        # before the watcher even gets scheduled still has a session to fall
        # back on.
        session = pane_session(pane, socket=socket, runner=runner)
        spawn(lambda: _await_and_followup(text, followup_sent, pane,
                                          socket=socket, runner=runner,
                                          session=session))

    return {"ok": True, "pane": pane, "text": text,
            "submitted": submitted, "followup": followup_sent,
            "followup_deferred": followup_deferred}
