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

import os
import secrets
import subprocess
import time
from typing import Any, Callable, Optional

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

# Longer settle between the slash command's Enter and the follow-up paste, so the
# composer has cleared (the command is on the queue) before the follow-up lands.
_FOLLOWUP_SETTLE_S = 0.3

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
    """List every pane on the tmux server as ``{pane, pid, command, session}``."""
    fmt = "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}"
    proc = _run(_base_argv(socket) + ["list-panes", "-a", "-F", fmt],
                runner, text=True)
    panes: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        panes.append({"pane": parts[0], "pid": parts[1],
                      "command": parts[2], "session": parts[3]})
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
    """Return the single tmux pane running an agent, or raise if ambiguous."""
    panes = list_panes(socket=socket, runner=runner)
    kids = _ppid_children()
    matches = [p for p in panes
               if p["pid"].isdigit() and _subtree_has_agent(int(p["pid"]), kids)]
    if len(matches) == 1:
        return matches[0]["pane"]
    if not matches:
        raise TmuxError(
            "could not auto-detect an agent pane; pass your $TMUX_PANE explicitly")
    cand = ", ".join(f'{p["pane"]} ({p["session"]})' for p in matches)
    raise TmuxError(
        f"multiple agent panes found ({cand}); pass your $TMUX_PANE explicitly")


def _assert_pane_exists(pane: str, socket: Optional[str], runner: Runner) -> None:
    _run(_base_argv(socket) + ["display-message", "-p", "-t", pane, "#{pane_id}"],
         runner, text=True)


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


def send(text: str, *, pane: Optional[str] = None, enter: bool = True,
         delay_ms: int = 0, confirm: bool = False,
         followup: Optional[str] = None,
         socket: Optional[str] = None,
         runner: Runner = subprocess.run) -> dict:
    """Paste ``text`` into ``pane`` and (unless ``enter=False``) submit it.

    Returns a result dict. Two guards can refuse before anything is pasted
    (``{"ok": False, "refused": True, ...}``):

    * A destructive command (see :data:`DESTRUCTIVE`) with ``confirm`` false.
    * A modal/interactive command (see :data:`opens_modal`) — these trap pasted
      input and would freeze the session; refused outright, no override.

    When a *submitted* slash command is not one of the above, a follow-up prompt
    is enforced: a bare slash command (e.g. ``/compact``) runs at end-of-turn and
    then leaves the session idle with nothing to do, so a trailing prompt is
    queued behind it to give the session a next turn. ``followup`` overrides the
    default text (:data:`DEFAULT_FOLLOWUP`). ``delay_ms`` waits before injecting;
    a leading ``/`` is safe thanks to bracketed paste.
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

    if delay_ms and delay_ms > 0:
        time.sleep(min(delay_ms, 25_000) / 1000.0)

    submitted = _paste_and_submit(text, pane, enter=enter,
                                  socket=socket, runner=runner)

    # A submitted slash command needs a real prompt behind it or the session
    # goes idle/dead once the command completes. Queue one (the caller's, or a
    # generic keep-alive). Plain prompts are their own turn — no follow-up.
    followup_sent = None
    if submitted and is_slash(text):
        followup_sent = (followup.strip() if followup and followup.strip()
                         else DEFAULT_FOLLOWUP)
        time.sleep(_FOLLOWUP_SETTLE_S)
        _paste_and_submit(followup_sent, pane, enter=True,
                          socket=socket, runner=runner)

    return {"ok": True, "pane": pane, "text": text,
            "submitted": submitted, "followup": followup_sent}
