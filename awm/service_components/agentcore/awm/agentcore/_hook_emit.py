#!/usr/bin/env python3
"""Claude Code hook emitter for the tmux backend — a control-file appender.

The interactive ``claude`` TUI emits no stream-json, so :class:`ClaudeTmuxSession`
observes it out-of-band: it injects SessionStart + Stop hooks (via ``--settings``)
that run *this* script. Claude invokes each hook with the hook payload JSON on
stdin (``{session_id, transcript_path, hook_event_name, ...}``); we append a
compact record to the control file named by ``$AWM_AGENTCORE_CONTROL_FILE`` (the
backend exports it in the launcher it ``exec``s ``claude`` from, so this hook
subprocess inherits it).

The backend tails that control file: the first ``SessionStart`` record hands it
the on-disk transcript JSONL path to follow; each ``Stop`` record marks a
per-turn boundary at which the backend synthesizes a terminal ``result`` event.

Pure stdlib; must exit 0 and write nothing to stdout that would block the turn.
"""
import json
import os
import sys
import time


def main() -> int:
    ctrl = os.environ.get("AWM_AGENTCORE_CONTROL_FILE")
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:  # noqa: BLE001
        payload = {"_parse_error": str(e), "_raw": raw[:200]}

    rec = {
        "event": payload.get("hook_event_name"),
        "transcript_path": payload.get("transcript_path"),
        "session_id": payload.get("session_id"),
        "ts": time.time(),
    }
    if ctrl:
        try:
            with open(ctrl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            # A missing/locked control file must never wedge the agent's turn.
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
