#!/usr/bin/env python3
"""Claude Code → awm-notifications producer hook.

Registered globally (``~/.claude/settings.json`` ``hooks``) for ``Stop``,
``Notification``, ``UserPromptSubmit``, ``SessionStart``, ``SessionEnd``.
Claude invokes it with the hook payload JSON on stdin
(``{session_id, transcript_path, hook_event_name, cwd, message?, reason?}``).

Contract (a hook must NEVER hurt the agent's turn):
- exit 0 always, write nothing to stdout;
- do the POST in a *detached child* so the parent returns immediately — a
  down/slow gateway can't stall the turn;
- do NOT read the transcript here (it lags the Stop hook — the flush race;
  the service reads it server-side with retry);
- scope filter: only report sessions whose cwd walk-up finds a ``.mcp.json``
  that loads the awm MCP (awm scope worktrees are ``.mcp.json``-free — the
  file lives at the workspace root above them, which the walk-up finds).

Endpoint: ``$AWM_HUB_URL`` when set (awm-spawned agents inherit it, so they
report to the hub that spawned them; exporting it also routes a sandbox e2e),
else prod ``http://127.0.0.1:7819``. ``AWM_NOTIFY_DISABLE=1`` kills the hook;
``AWM_NOTIFY_SCOPE=any`` disables the cwd filter.

Pure stdlib.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

_EVENT_MAP = {
    "Stop": "turn_end",
    "Notification": "notification",
    "UserPromptSubmit": "user_prompt",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
}


def _in_awm_scope(cwd: str | None) -> bool:
    """Walk up from cwd looking for a .mcp.json that loads the awm MCP."""
    if os.environ.get("AWM_NOTIFY_SCOPE") == "any":
        return True
    if os.environ.get("AWM_HUB_URL"):
        return True  # awm-spawned agent — always in scope
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        candidate = os.path.join(d, ".mcp.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    servers = (json.load(f) or {}).get("mcpServers") or {}
                if any("awm" in str(k).lower() for k in servers):
                    return True
            except Exception:  # noqa: BLE001
                pass
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _send(payload_json: str) -> int:
    """Child mode: POST the payload, swallow every error."""
    hub = (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/")
    req = urllib.request.Request(
        f"{hub}/svc/notifications/fn/report",
        data=payload_json.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:  # noqa: BLE001 — fire-and-forget
        pass
    return 0


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--send":
        return _send(sys.argv[2])

    if os.environ.get("AWM_NOTIFY_DISABLE") == "1":
        return 0
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return 0

    event = _EVENT_MAP.get(payload.get("hook_event_name") or "")
    session_id = payload.get("session_id")
    if not event or not session_id:
        return 0
    cwd = payload.get("cwd")
    if not _in_awm_scope(cwd):
        return 0

    report = {
        "harness": "claude",
        "event": event,
        "session_id": session_id,
        "cwd": cwd,
    }
    if payload.get("transcript_path"):
        report["transcript_path"] = payload["transcript_path"]
    if payload.get("message"):
        report["message"] = payload["message"]
    if payload.get("reason"):
        report["reason"] = payload["reason"]

    # Detach: the child does the POST; we exit now so the turn never waits.
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--send",
             json.dumps(report)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
