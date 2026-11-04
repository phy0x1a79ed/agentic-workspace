"""Tell reflection how a command with a follow-up ended (harness-contract.md § Events).

cch watches a ``/compact`` or any other command with a follow-up through to its
end, then calls ``reflection`` fn ``harness_event`` with ``done`` or ``lost``.
reflection keeps the ledger and raises the alarm. cch types the lost notice into
a live session itself, because typing is harness work.

An event that cannot reach reflection is spooled to ``unsent.jsonl`` beside the
promises and sent again on the next start, so a lost resume is never lost twice.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from awm.cch import pending
from awm.gatewayclient import ids

log = logging.getLogger("awm.cch.events")

PLATFORM = "cc"
UNSENT_FILE = "unsent.jsonl"

# Overridable for tests: (service, fn, args) -> result.
_call: Optional[Callable[[str, str, dict], Any]] = None


def r9(session_uuid: str) -> str:
    """The R9 id of a Claude Code session UUID (`sessions.md` R9)."""
    return ids.r9(PLATFORM, session_uuid)


def _send(event: dict) -> Any:
    if _call is not None:
        return _call("reflection", "harness_event", event)
    from awm.gatewayclient import call_sync
    return call_sync("reflection", "harness_event", event, timeout=15.0)


def _cwd(pid: int) -> Optional[str]:
    from awm.cch import transcript
    try:
        return transcript.session_and_cwd_for(pid)[1]
    except Exception:  # noqa: BLE001 - an event never raises
        return None


def notice(item: pending.Pending, reason: str) -> str:
    return (f"[awm reflection] Your `{item.text}` and the follow-up "
            f"\"{item.followup}\" were lost: {reason}. Nothing is queued for you "
            f"now. Re-run the command if you still need it, then carry on. "
            f"Automated message from the reflection service; it is not a new "
            f"instruction from the user.")


def _event(item: pending.Pending, kind: str, reason: str, **extra: Any) -> dict:
    return {"session_id": r9(item.session_id) if item.session_id else "",
            "ticket": item.ticket, "kind": kind, "command": item.text,
            "followup": item.followup, "reason": reason,
            "name": item.name or "", "pid": item.repl_pid,
            "injected_at_ms": item.injected_at_ms, "reissues": item.reissues,
            **extra}


def _report(event: dict) -> bool:
    try:
        reply = _send(event)
    except Exception as exc:  # noqa: BLE001
        reply = {"ok": False, "error": str(exc)}
    if isinstance(reply, dict) and reply.get("ok") is not False:
        return True
    log.error("cch: reflection did not take the %s event for session %s: %s; "
              "spooled to resend", event["kind"], event["session_id"],
              (reply or {}).get("error") if isinstance(reply, dict) else reply)
    try:
        path = pending.dir_path() / UNSENT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event) + "\n")
    except OSError as exc:
        log.error("cch: could not spool the %s event either: %s", event["kind"], exc)
    return False


def done(item: pending.Pending, reason: str = "") -> bool:
    """The session took the follow-up up after the command finished."""
    return _report(_event(item, "done", reason))


def lost(item: pending.Pending, reason: str, *,
         alive_pid: Optional[int] = None,
         deliver: Optional[Callable[..., Any]] = None,
         cwd: Optional[str] = None) -> dict:
    """Give up on ``item`` out loud: an ERROR line, a notice into a live session, an event."""
    who = item.name or item.session_id or f"pid {item.repl_pid}"
    log.error("cch: LOST %s for session %s: %s", item.text, who, reason)
    told = False
    if alive_pid and deliver is not None:
        try:
            deliver(alive_pid, notice(item, reason), enter=True,
                    what="the lost-resume notice")
            told = True
        except Exception as exc:  # noqa: BLE001
            log.warning("cch: could not tell session %s its resume was lost: %s",
                        who, exc)
    where = cwd if cwd is not None else _cwd(alive_pid or item.repl_pid)
    sent = _report(_event(item, "lost", reason, cwd=where or "",
                          told_session=told, at_ms=int(time.time() * 1000)))
    return {"told_session": told, "reported": sent}


def resend_unsent() -> int:
    """Send the spooled events again. Returns how many reflection took."""
    path = pending.dir_path() / UNSENT_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        path.unlink()
    except OSError:
        return 0
    sent = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        sent += _report(event)
    return sent
