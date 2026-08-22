"""The tmux lane: type into an interactive session's pane, and read it back.

An agent running the interactive ``claude`` TUI inside a tmux pane cannot type a
control command into itself — but any process on the host, as the same user, can
paste one into that pane. tmux buffers the input and the TUI runs it the instant
the current turn ends, which is exactly when a self-directed ``/compact`` should
fire.

The paste sequence mirrors ``awm.agentcore.claude_backend._paste_prompt``:
``load-buffer`` (stdin) → ``paste-buffer -d -p`` (``-p`` = *bracketed* paste, so a
leading ``/`` lands as literal text instead of opening the TUI's slash menu) →
``send-keys Enter``. Crucially it sends **no Escape** — Escape would cancel the
agent's in-flight turn (that is ``claude_backend.interrupt``'s job); we want the
command to *queue* behind the current turn, not interrupt it.

This module knows tmux and nothing else. Which session to type into is
:mod:`awm.reflection.session_target`'s job, how many times to try is
:mod:`awm.reflection.inject`'s, and waiting for the command to finish is
:mod:`awm.reflection.watcher`'s — that last one used to live here as a
screen-scraping poller, and having two of those (one per transport) is how they
drifted apart until only one of them worked.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("awm.reflection.tmux_inject")

# Settle beat between the paste landing and the Enter that submits it, so the
# TUI has focused the pasted input before we press return — and between a write
# and the read-back that verifies it, so we classify the redrawn prompt rather
# than the one we just invalidated.
_SETTLE_S = 0.15

# Ctrl-U: kill the prompt line. Only ever sent before a *retry*, to wipe whatever
# a failed attempt left in the box so the next paste cannot concatenate onto it.
_CLEAR_KEY = "C-u"

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
# Pane discovery
# ---------------------------------------------------------------------------

def list_panes(*, socket: Optional[str] = None,
               runner: Runner = subprocess.run) -> list[dict]:
    """List every pane on the tmux server as ``{pane, pid, command, session}``.

    Note there is no activity/recency field here on purpose. An earlier version
    asked tmux for ``#{pane_activity}`` to rank candidate panes; that format does
    not exist (tmux offers ``window_activity``, not a per-pane one), so it always
    expanded to the empty string and every pane ranked identically. Ranking panes
    by recency is not something to reach for again — the caller's identity says
    which pane is theirs, and nothing else should get a vote.
    """
    fmt = ("#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}")
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


def _subtree_contains(root_pid: int, target_pid: int,
                      kids: dict[int, list[int]]) -> bool:
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid == target_pid:
            return True
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(kids.get(pid, []))
    return False


def pane_for_pid(pid: int, *, socket: Optional[str] = None,
                 runner: Runner = subprocess.run) -> Optional[str]:
    """Return the pane whose process subtree contains ``pid``, or ``None``.

    This is how a caller's own pane is identified: an exact ancestry match
    against a pid we were *given*, not a search for something that looks like an
    agent. Exactly one pane can contain a given pid, so there is no ambiguity to
    resolve and nothing to rank.
    """
    kids = _ppid_children()
    for p in list_panes(socket=socket, runner=runner):
        if p["pid"].isdigit() and _subtree_contains(int(p["pid"]), pid, kids):
            return p["pane"]
    return None


def _assert_pane_exists(pane: str, socket: Optional[str], runner: Runner) -> None:
    _run(_base_argv(socket) + ["display-message", "-p", "-t", pane, "#{pane_id}"],
         runner, text=True)


def _assert_pane_has_agent(pane: str, socket: Optional[str], runner: Runner) -> None:
    """Refuse to inject into a pane whose process subtree has no agent.

    Guards against the one case pane resolution cannot rule out on its own: the
    pane id is real and exists, but nothing running there is a `claude`/`opencode`
    process (a stale id now repointed at a shell or editor).
    """
    proc = _run(_base_argv(socket) + ["display-message", "-p", "-t", pane,
                                       "#{pane_pid}"], runner, text=True)
    pid_s = (proc.stdout or "").strip()
    if not pid_s.isdigit() or not _subtree_has_agent(int(pid_s), _ppid_children()):
        raise TmuxError(
            f"pane {pane} is not running an agent (claude/opencode); "
            f"refusing to inject")


def capture_pane(pane: str, *, socket: Optional[str] = None,
                 runner: Runner = subprocess.run) -> str:
    """Return the visible text of ``pane`` (``tmux capture-pane -p``)."""
    proc = _run(_base_argv(socket) + ["capture-pane", "-t", pane, "-p"],
                runner, text=True)
    return proc.stdout or ""


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------

class _TmuxWriter:
    """Write to, and read back from, one pane.

    The four verbs are the same four the daemon lane offers, because
    :mod:`awm.reflection.inject` drives both through this shape and must not know
    which one it is holding.
    """

    def __init__(self, lane, *, socket: Optional[str], runner: Runner,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._lane = lane
        self._socket = socket
        self._runner = runner
        self._sleep = sleep

    # `capture-pane` renders the pane's *current* state, so a probe that is not
    # in the read-back is genuinely not on screen. That makes a negative read
    # here real evidence — see the daemon lane, where it is not.
    read_back_is_evidence = True

    @property
    def label(self) -> str:
        return f"pane {self._lane.pane}"

    def read_back(self) -> str:
        return capture_pane(self._lane.pane, socket=self._socket,
                            runner=self._runner)

    def clear(self) -> None:
        _run(_base_argv(self._socket)
             + ["send-keys", "-t", self._lane.pane, _CLEAR_KEY], self._runner)
        self._sleep(_SETTLE_S)

    def write(self, text: str) -> None:
        buf = "awm-rfl-" + secrets.token_hex(4)
        _run(_base_argv(self._socket) + ["load-buffer", "-b", buf, "-"],
             self._runner, input=text.encode("utf-8"), capture_output=True)
        _run(_base_argv(self._socket)
             + ["paste-buffer", "-d", "-p", "-b", buf, "-t", self._lane.pane],
             self._runner)
        self._sleep(_SETTLE_S)

    def commit(self) -> None:
        _run(_base_argv(self._socket)
             + ["send-keys", "-t", self._lane.pane, "Enter"], self._runner)


@contextmanager
def open_lane(lane, *, socket: Optional[str] = None,
              runner: Runner = subprocess.run,
              sleep: Callable[[float], None] = time.sleep
              ) -> Iterator[_TmuxWriter]:
    """Open ``lane`` for writing, checking it is still a live agent pane.

    Both assertions belong to *this attempt*, not to some earlier resolution: the
    pane id came from a detector call made moments ago, and a pane can be
    destroyed or repointed at a shell in between. Re-checking here is what makes
    each retry a fresh transaction rather than a repeat of a stale one.
    """
    _assert_pane_exists(lane.pane, socket, runner)
    _assert_pane_has_agent(lane.pane, socket, runner)
    yield _TmuxWriter(lane, socket=socket, runner=runner, sleep=sleep)
