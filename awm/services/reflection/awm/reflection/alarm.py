"""Say out loud that a compaction or a resume was lost.

A lost resume leaves a session idle with nobody told. So each ``lost`` event a
harness plugin sends ends here, and :func:`raise_alarm` leaves three traces: an
ERROR log line, a line in ``lost.jsonl`` that ``reflection pending`` lists, and
a journal message to the session's scope, which copies onto its card. The
harness plugin types the notice into a live session itself. Each trace is best
effort, and an alarm never raises.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("awm.reflection.alarm")

LOST_FILE = "lost.jsonl"
# The newest lost entries `reflection pending` shows.
SHOWN = 20

# Overridable for tests.
STATE_DIR: Optional[Path] = None
# Overridable for tests: (service, fn, args) -> result.
_call: Optional[Callable[[str, str, dict], Any]] = None


def state_dir() -> Path:
    if STATE_DIR is not None:
        return STATE_DIR
    from awm.config import SERVICES_DIR
    return SERVICES_DIR / "reflection"


def _gateway_call(service: str, fn: str, args: dict) -> Any:
    if _call is not None:
        return _call(service, fn, args)
    from awm.gatewayclient import call_sync
    return call_sync(service, fn, args, timeout=10.0)


def lost_path() -> Path:
    return state_dir() / LOST_FILE


def scope_of(cwd: Optional[str]) -> Optional[tuple[str, str]]:
    """``(project, scope)`` for a worktree under ``<workspace>/projects/``, else None."""
    if not cwd:
        return None
    parts = Path(cwd).parts
    if "projects" not in parts:
        return None
    rest = parts[len(parts) - parts[::-1].index("projects"):]
    if len(rest) < 2 or rest[0].startswith("."):
        return None
    return rest[0], "/".join(rest[1:])


def raise_alarm(event: dict) -> dict:
    """Record and announce the ``lost`` event. Returns what it did."""
    who = event.get("name") or event.get("session_id") or f"pid {event.get('pid')}"
    command, followup = event.get("command") or "", event.get("followup") or ""
    reason = event.get("reason") or "no reason given"
    did: dict[str, Any] = {"logged": True, "recorded": False, "journal": False}
    log.error("reflection: LOST %s for session %s: %s", command, who, reason)
    entry = {"at_ms": event.get("at_ms") or int(time.time() * 1000),
             "session": event.get("name") or "",
             "session_id": event.get("session_id") or "",
             "pid": event.get("pid"), "command": command, "resume": followup,
             "ticket": event.get("ticket") or "",
             "injected_at_ms": event.get("injected_at_ms"),
             "reissues": event.get("reissues") or 0,
             "told_session": bool(event.get("told_session")), "reason": reason}
    try:
        path = lost_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry) + "\n")
        did["recorded"] = True
    except OSError as exc:
        log.warning("reflection: could not record the lost resume: %s", exc)

    where = scope_of(event.get("cwd"))
    if where:
        project, scope = where
        try:
            _gateway_call("journal", "post", {
                "project": project, "scope": scope, "author": "reflection",
                "kind": "message",
                "meta": {"alarm": "reflection-lost",
                         "session_id": entry["session_id"], "command": command},
                "body": (f"reflection lost `{command}` for session {who}: "
                         f"{reason}. The session was owed the follow-up "
                         f"\"{followup}\" and did not get it."),
            })
            did["journal"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("reflection: could not post the lost-resume alarm to "
                        "%s/%s: %s", project, scope, exc)
    return did


def recent(limit: int = SHOWN) -> list[dict]:
    """The newest lost entries, newest first."""
    try:
        lines = lost_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
