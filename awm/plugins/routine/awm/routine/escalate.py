"""The failure chain of routines.md R47 and R48.

1. Start the fixing agent on the declared peer, when the routine declares one.
2. Otherwise, or when that fails, post a notice to the operator's channel.
3. When that fails too, log locally and stop.

The chain never raises: a failure path that throws would hide the failure it
reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from awm.routine.outbound import Outbound

log = logging.getLogger("awm.routine.escalate")


@dataclass
class Escalation:
    via: str                 # "agent" | "notice" | "log"
    job: str = ""
    peer: str = ""
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"via": self.via, "job": self.job, "peer": self.peer,
                "errors": list(self.errors)}


async def escalate(out: Outbound, *, peer: str, project: str, scope: str,
                   name: str, prompt: str, started_by: str,
                   summary: str) -> Escalation:
    errors: list[str] = []
    try:
        res = await out.start_session(peer=peer or "local", project=project,
                                      scope=scope, prompt=prompt,
                                      started_by=started_by, name=name)
        return Escalation(via="agent", job=str(res.get("job", "")), peer=peer)
    except Exception as e:  # noqa: BLE001 — every failure moves down the chain
        errors.append(f"start on {peer or 'local'}: {e}")
        log.warning("fixer start failed on %s: %s", peer, e)
    return await notice(out, f"{summary}\nThe fixing agent could not start on "
                             f"{peer or 'local'}: {errors[-1]}", peer=peer, errors=errors)


async def notice(out: Outbound, text: str, *, peer: str = "",
                 errors: list[str] | None = None) -> Escalation:
    """Post `text` to the operator's channel, or log it when that fails."""
    errors = list(errors or ())
    try:
        await out.notify_operator(text)
        return Escalation(via="notice", peer=peer, errors=tuple(errors))
    except Exception as e:  # noqa: BLE001
        errors.append(f"operator notice: {e}")
    log.error("routine failure with no agent and no notice: %s | %s", text, errors)
    return Escalation(via="log", peer=peer, errors=tuple(errors))
