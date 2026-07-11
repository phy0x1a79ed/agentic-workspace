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
  the service reads it server-side with retry).

Roster is **machine-wide**: every Claude session reports, not just awm-scope
worktrees (the fleet page shows the whole machine; non-tmux sessions still
appear, just marked not-attachable). When ``$TMUX`` is set the hook resolves the
enclosing tmux session name and sends it as ``tmux_session`` — the browser
terminal's attach handle.

Endpoint: ``$AWM_HUB_URL`` when set (awm-spawned agents inherit it, so they
report to the hub that spawned them; exporting it also routes a sandbox e2e),
else prod ``http://127.0.0.1:7819``. ``AWM_NOTIFY_DISABLE=1`` kills the hook.

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


def _tmux_session() -> str | None:
    """The enclosing tmux session name, best-effort (None when not in tmux).

    ``TMUX_PANE`` is set per-pane inside tmux, so ``display-message -t <pane>``
    resolves *this* pane's session unambiguously even under a shared server.
    Cheap + bounded; a failure just yields None (session shows not-attachable).
    """
    if not os.environ.get("TMUX"):
        return None
    args = ["tmux", "display-message", "-p"]
    pane = os.environ.get("TMUX_PANE")
    if pane:
        args += ["-t", pane]
    args += ["#S"]
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=2)
        name = (out.stdout or "").strip()
        return name or None
    except Exception:  # noqa: BLE001
        return None


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

    report = {
        "harness": "claude",
        "event": event,
        "session_id": session_id,
        "cwd": cwd,
    }
    tmux = _tmux_session()
    if tmux:
        report["tmux_session"] = tmux
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
