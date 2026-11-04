#!/usr/bin/env python3
"""The awm SessionStart hook: the scope's brief, and the parent link of a clear.

The brief (scopes.md R143): the scope is the git worktree under
``<workspace>/projects/`` that holds the session's folder. The brief comes from
`journal brief_read` and goes into the session's context. A session outside every
scope, a scope with no brief or a gateway that does not answer adds nothing.

The clear (sessions.md R13): Claude Code gives a cleared session a new id and no
word of the old one, so the hook keeps the last id it saw for each Claude Code
process. On a ``clear`` it tells reflection the new id and the old, as a
``harness_event`` of kind ``clear``. Every path exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 4.0
# The clear's report shares the hook's 6-second budget with the brief.
CLEAR_TIMEOUT_S = 1.5


def scope_of(cwd: str) -> tuple[str, str] | None:
    """``(project, scope)`` of the worktree that holds ``cwd``, or None."""
    path = os.path.realpath(cwd)
    parts = path.split(os.sep)
    if "projects" not in parts:
        return None
    root = len(parts) - parts[::-1].index("projects")
    here = path
    while True:
        rel = here.split(os.sep)[root:]
        if len(rel) < 2 or rel[0].startswith("."):
            return None
        if os.path.exists(os.path.join(here, ".git")):
            return rel[0], "/".join(rel[1:])
        here = os.path.dirname(here)


def _invoke(tool: str, args: dict, timeout: float = TIMEOUT_S) -> object:
    url = (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/") + "/invoke"
    body = json.dumps({"name": tool, "args": args}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Awm-Caller-Pid": str(os.getppid())})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode() or "{}").get("result")
        return json.loads(result) if isinstance(result, str) else result
    except (urllib.error.URLError, OSError, ValueError):
        return None


def brief(project: str, scope: str) -> str:
    result = _invoke("journal_brief_read", {"project": project, "scope": scope})
    got = result.get("brief") if isinstance(result, dict) else None
    return got.get("text") or "" if isinstance(got, dict) else ""


def _ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as fp:
            return int(fp.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def claude_pid() -> int | None:
    """The Claude Code process that ran this hook: the nearest ancestor with a session record."""
    records = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR")
                           or os.path.expanduser("~/.claude"), "sessions")
    pid = os.getppid()
    for _ in range(6):
        if pid <= 1:
            return None
        if os.path.exists(os.path.join(records, f"{pid}.json")):
            return pid
        pid = _ppid(pid)
    return None


def seen_dir() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "awm", "cch", "session-start")


def swap_seen(pid: int, session_id: str) -> str:
    """Record ``session_id`` as the last id of ``pid``, and answer the one before it.

    Drops the records of processes that are gone, so the folder stays small.
    """
    folder = seen_dir()
    path = os.path.join(folder, str(pid))
    try:
        with open(path) as fp:
            before = fp.read().strip()
    except OSError:
        before = ""
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w") as fp:
            fp.write(session_id)
        for name in os.listdir(folder):
            if name.isdigit() and not os.path.exists(f"/proc/{name}"):
                os.unlink(os.path.join(folder, name))
    except OSError:
        pass
    return before


def report_clear(session_id: str, parent: str) -> None:
    _invoke("reflection_harness_event",
            {"session_id": session_id, "kind": "clear", "parent": parent},
            timeout=CLEAR_TIMEOUT_S)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    pid = claude_pid() if isinstance(session_id, str) and session_id else None
    if pid:
        before = swap_seen(pid, session_id)
        if payload.get("source") == "clear" and before and before != session_id:
            report_clear(session_id, before)
    cwd = payload.get("cwd")
    where = scope_of(cwd) if isinstance(cwd, str) and cwd else None
    text = brief(*where) if where else ""
    if text:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"The brief of {where[0]}/{where[1]}: {text}"}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook must never cost a session its start
        pass
