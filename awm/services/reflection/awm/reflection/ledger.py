"""The ledger of owed follow-ups: which tickets a harness plugin still owes, and how each ended.

A harness plugin answers a command with a follow-up with a ticket, then sends
``harness_event`` once the session took the follow-up up (``done``) or it gave
up (``lost``). A ticket that its plugin no longer holds, and that never got an
event, is lost too: :func:`sweep` finds those and raises the alarm.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from awm.reflection import alarm

log = logging.getLogger("awm.reflection.ledger")

EVENTS_FILE = "events.jsonl"
# A ticket younger than this is not swept: its plugin may not have recorded it yet.
SETTLE_S = 300


def _tickets() -> Path:
    return alarm.state_dir() / "tickets"


def open_ticket(ticket: str, *, session_id: str, tool: str, command: str,
                followup: str, name: str = "", cwd: Optional[str] = None) -> None:
    if not ticket or (_tickets() / f"{ticket}.closed").exists():
        return
    rec = {"ticket": ticket, "session_id": session_id, "tool": tool,
           "command": command, "followup": followup, "name": name, "cwd": cwd,
           "opened_ms": int(time.time() * 1000)}
    try:
        _tickets().mkdir(parents=True, exist_ok=True)
        (_tickets() / f"{ticket}.json").write_text(json.dumps(rec))
    except OSError as exc:
        log.warning("reflection: could not record ticket %s: %s", ticket, exc)


def open_tickets() -> list[dict]:
    out = []
    try:
        paths = sorted(_tickets().glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


def _close(ticket: str) -> Optional[dict]:
    """The open record of ``ticket``, removed. An event can beat its own ticket
    here, so a ticket closed before it opened leaves a marker that keeps it shut."""
    path = _tickets() / f"{ticket}.json"
    try:
        rec = json.loads(path.read_text())
        path.unlink()
        return rec
    except (OSError, ValueError):
        pass
    try:
        _tickets().mkdir(parents=True, exist_ok=True)
        (_tickets() / f"{ticket}.closed").touch()
    except OSError:
        pass
    return None


def record(event: dict) -> dict:
    """Close the event's ticket, keep the event, and raise the alarm on ``lost``."""
    opened = _close(event.get("ticket") or "") if event.get("ticket") else None
    full = {**(opened or {}), **{k: v for k, v in event.items() if v not in (None, "")}}
    try:
        path = alarm.state_dir() / EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps({**full, "received_ms": int(time.time() * 1000)}) + "\n")
    except OSError as exc:
        log.warning("reflection: could not keep the %s event: %s", full.get("kind"), exc)
    if full.get("kind") == "lost":
        return {"ok": True, "alarm": alarm.raise_alarm(full)}
    if full.get("kind") != "done":
        return {"ok": False, "error": f"unknown event kind {full.get('kind')!r}"}
    return {"ok": True}


def sweep(held: Callable[[str], Optional[Iterable[str]]],
          now_ms: Optional[int] = None) -> list[str]:
    """Raise the alarm for every settled ticket its plugin no longer holds.

    ``held(tool)`` answers the tickets the plugin still holds, or None when the
    plugin cannot be asked: a ticket is never declared lost on no answer.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    asked: dict[str, Optional[set[str]]] = {}
    lost = []
    for rec in open_tickets():
        if now_ms - int(rec.get("opened_ms") or now_ms) < SETTLE_S * 1000:
            continue
        tool = rec.get("tool") or ""
        if tool not in asked:
            got = held(tool)
            asked[tool] = set(got) if got is not None else None
        if asked[tool] is None or rec["ticket"] in asked[tool]:
            continue
        record({**rec, "kind": "lost",
                "reason": (f"{tool} no longer holds the follow-up and never "
                           f"reported how it ended")})
        lost.append(rec["ticket"])
    return lost
