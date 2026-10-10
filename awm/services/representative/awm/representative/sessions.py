"""The door's view of the node's sessions, through the cx service.

The door never reads the daemon roster itself. It asks `cx list` which sessions
exist, calls `cx start` for a missing one, and calls `cx stop` only on a session
it started that has failed. Why a live session cannot work comes from the
`needs` and `detail` fields of its `cx list` row.

A session counts as the representative or the secretary only if the door
started it (the door records each job id `cx start` returns) and it carries the
role's *mode* label. The label alone is not trusted: another caller could start
a session with it. The mode, unlike a name, survives a rename from the Claude
app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from awm import gatewayclient

log = logging.getLogger("awm.representative.sessions")

START_TIMEOUT_S = 240.0
LIST_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 60.0
GONE = "gone"
#: Job states (`cx list` `state`) that can mean the session cannot work. Claude
#: Code sets them from an API error and also from the session's own last line
#: ("failed: ...", "blocked: ...", a closing question), so the `needs` text
#: decides which they are. See `classify`.
BLOCKED = "blocked"
FAILED = "failed"
WAITING = "waiting"
#: The `needs` Claude Code writes for an API error it cannot classify. A new
#: session is the only mend.
API_ERROR = "API error"
#: Starts of the `needs` texts that a person must clear. Rate limits, overload,
#: server errors and a session's own question clear on the next prompt.
PERSON_NEEDS = (
    "login required", "org disabled oauth", "account on hold",
    "cloud credentials unavailable", "organization verification required",
    "usage limit reached",
)


class CxUnavailable(RuntimeError):
    """cx did not give a usable answer, so nothing can be said about the sessions."""


class Cx:
    """A thin client for the two cx verbs the door uses."""

    async def list(self) -> list[dict]:
        try:
            reply = await gatewayclient.call("cx", "list", {}, timeout=LIST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "cannot tell"
            raise CxUnavailable(f"cx list failed: {exc}") from exc
        if not isinstance(reply, dict) or reply.get("ok") is False or "sessions" not in reply:
            reason = reply.get("reason") if isinstance(reply, dict) else None
            raise CxUnavailable(f"cx list refused: {reason or reply!r}")
        return [row for row in reply["sessions"] if isinstance(row, dict)]

    async def start(self, spec: dict[str, Any]) -> dict:
        args = {k: v for k, v in spec.items() if v is not None}
        try:
            reply = await gatewayclient.call("cx", "start", args, timeout=START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx start failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}


    async def stop(self, job: str) -> dict:
        try:
            reply = await gatewayclient.call("cx", "stop", {"job": job}, timeout=STOP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx stop failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}


def _live_with_mode(rows: list[dict], mode: str) -> list[dict]:
    return [r for r in rows if r.get("mode") == mode and r.get("state") != GONE]


def holders(rows: list[dict], mode: str, jobs: set[str]) -> list[dict]:
    """Live sessions with the mode label *that the door started* (``jobs`` is its record)."""
    return [r for r in _live_with_mode(rows, mode) if r.get("job") in jobs]


def strangers(rows: list[dict], mode: str, jobs: set[str]) -> list[dict]:
    """Live sessions that carry the mode label but were not started by the door."""
    return [r for r in _live_with_mode(rows, mode) if r.get("job") not in jobs]


def find_job(rows: list[dict], mode: str, jobs: set[str]) -> str | None:
    """The job id of the door's live session with this mode, or None."""
    for row in holders(rows, mode, jobs):
        if row.get("job"):
            return row["job"]
    return None


# -- health -------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """Whether a role's session can do its work, and if not, why.

    ``state`` is `running`, `waiting`, `blocked`, `failed`, `exited` or `missing`.
    ``reason`` is short: `auth_required`, Claude Code's own `needs` text, or "".
    `running` and `waiting` are alive. `waiting` is a block that clears on the
    next prompt. `blocked` needs a person. `failed` is the one state a restart mends.
    """

    state: str
    reason: str = ""
    job: str | None = None

    @property
    def alive(self) -> bool:
        return self.state in ("running", WAITING)

    @property
    def restartable(self) -> bool:
        return self.state == FAILED

    def as_dict(self) -> dict[str, Any]:
        return {"alive": self.alive, "state": self.state, "reason": self.reason, "job": self.job}


def _short(text: Any, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


def classify(row: dict) -> Health:
    """Health of one live session from its `cx list` row.

    An expired login is `state: blocked` with `needs: "login required \u2014 run
    /login"`. Claude Code also sets `blocked` and `failed` from the session's own
    last line, so only the harness's own `needs` texts count: `failed` with
    `API error` is restartable, and `blocked` with a `PERSON_NEEDS` text is not
    alive. Any other `blocked` is `waiting`, and any other `failed` is `running`.
    A state the door has not seen counts as running, so an unfamiliar value never
    starts a restart.
    """
    job = row.get("job")
    state = row.get("state")
    needs = _short(row.get("needs"))
    if state == FAILED and needs == API_ERROR:
        return Health(FAILED, API_ERROR, job)
    if state != BLOCKED:
        return Health("running", "", job)
    lowered = needs.lower()
    if lowered.startswith("login required") or "login expired" in str(row.get("detail") or "").lower():
        return Health(BLOCKED, "auth_required", job)
    if lowered.startswith(PERSON_NEEDS):
        return Health(BLOCKED, needs, job)
    return Health(WAITING, needs or "needs input", job)


def assess(rows: list[dict], mode: str, jobs: set[str]) -> Health:
    """Health of the role whose label is ``mode`` and whose session the door started (``jobs``).

    A session that can work wins over one that cannot when several carry the label.
    With none live, `exited` means the door has started one that is gone now and
    `missing` means it has none to show for it.
    """
    mine = holders(rows, mode, jobs)
    if mine:
        found = [classify(r) for r in mine]
        return next((h for h in found if h.alive), found[0])
    gone = [r for r in rows if r.get("job") in jobs and r.get("state") == GONE]
    return Health("exited", "", gone[-1].get("job")) if gone else Health("missing")
