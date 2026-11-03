"""Say out loud that a compaction or a resume was lost.

A lost resume leaves a session idle with nobody told. The service log was the
only trace, and nobody reads it. So every give-up path calls :func:`raise_alarm`,
which leaves four traces: an ERROR log line, a line in ``lost.jsonl`` that
``reflection pending`` lists, a journal message to the session's scope (which
copies onto its card), and, while the session is alive, one plain line typed
into it. Each trace is best effort, and an alarm never raises.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from awm.reflection import pending

log = logging.getLogger("awm.reflection.alarm")

LOST_FILE = "lost.jsonl"
# The newest lost entries `reflection pending` shows.
SHOWN = 20

# Overridable for tests: (service, fn, args) -> result.
_call: Optional[Callable[[str, str, dict], Any]] = None


def _gateway_call(service: str, fn: str, args: dict) -> Any:
    if _call is not None:
        return _call(service, fn, args)
    from awm.gatewayclient import call_sync
    return call_sync(service, fn, args, timeout=10.0)


def lost_path() -> Path:
    return pending.dir_path() / LOST_FILE


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


def _session_cwd(repl_pid: int) -> Optional[str]:
    from awm.reflection import transcript
    return transcript.session_and_cwd_for(repl_pid)[1]


def notice(item: pending.Pending, reason: str) -> str:
    return (f"[awm reflection] Your `{item.text}` and the follow-up "
            f"\"{item.followup}\" were lost: {reason}. Nothing is queued for you "
            f"now. Re-run the command if you still need it, then carry on. "
            f"Automated message from the reflection service; it is not a new "
            f"instruction from the user.")


def raise_alarm(item: pending.Pending, reason: str, *,
                alive_pid: Optional[int] = None,
                deliver: Optional[Callable[..., Any]] = None,
                cwd: Optional[str] = None) -> dict:
    """Record and announce that ``item`` was lost for ``reason``. Returns what it did."""
    who = item.name or item.session_id or f"pid {item.repl_pid}"
    did: dict[str, Any] = {"logged": True, "recorded": False, "journal": False,
                           "told_session": False}
    log.error("reflection: LOST %s for session %s: %s", item.text, who, reason)
    entry = {"at_ms": int(time.time() * 1000), "session": item.name or "",
             "session_id": item.session_id, "pid": item.repl_pid,
             "command": item.text, "resume": item.followup,
             "injected_at_ms": item.injected_at_ms, "reissues": item.reissues,
             "reason": reason}
    try:
        path = lost_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry) + "\n")
        did["recorded"] = True
    except OSError as exc:
        log.warning("reflection: could not record the lost resume: %s", exc)

    try:
        where = scope_of(cwd if cwd is not None else _session_cwd(
            alive_pid or item.repl_pid))
    except Exception:  # noqa: BLE001 - an alarm never raises
        where = None
    if where:
        project, scope = where
        try:
            _gateway_call("journal", "post", {
                "project": project, "scope": scope, "author": "reflection",
                "kind": "message",
                "meta": {"alarm": "reflection-lost", "session_id": item.session_id,
                         "command": item.text},
                "body": (f"reflection lost `{item.text}` for session {who}: "
                         f"{reason}. The session was owed the follow-up "
                         f"\"{item.followup}\" and did not get it."),
            })
            did["journal"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("reflection: could not post the lost-resume alarm to "
                        "%s/%s: %s", project, scope, exc)

    if alive_pid and deliver is not None:
        try:
            deliver(alive_pid, notice(item, reason), enter=True,
                    what="the lost-resume notice")
            did["told_session"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("reflection: could not tell session %s its resume was "
                        "lost: %s", who, exc)
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
