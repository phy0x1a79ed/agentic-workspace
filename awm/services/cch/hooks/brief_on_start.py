#!/usr/bin/env python3
"""Give a Claude Code session the brief of the awm scope it starts in (scopes.md R143).

Runs on SessionStart. The scope is the git worktree under ``<workspace>/projects/``
that holds the session's folder. The brief comes from `journal brief_read` and goes
into the session's context. A session outside every scope, a scope with no brief or
a gateway that does not answer adds nothing. Every path exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 4.0


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


def brief(project: str, scope: str) -> str:
    url = (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/") + "/invoke"
    body = json.dumps({"name": "journal_brief_read",
                       "args": {"project": project, "scope": scope}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Awm-Caller-Pid": str(os.getppid())})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode() or "{}").get("result")
        if isinstance(result, str):
            result = json.loads(result)
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    got = result.get("brief") if isinstance(result, dict) else None
    return got.get("text") or "" if isinstance(got, dict) else ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
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
