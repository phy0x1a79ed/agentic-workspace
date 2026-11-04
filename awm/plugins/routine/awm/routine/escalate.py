"""The failure chain of routines.md R47 and R48.

1. Start the fixing agent on the declared peer.
2. When that fails, DM the operator on Discord.
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
    via: str                 # "agent" | "dm" | "log"
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
    try:
        await out.notify_operator(
            f"{summary}\nThe fixing agent could not start on {peer or 'local'}: "
            f"{errors[-1]}")
        return Escalation(via="dm", peer=peer, errors=tuple(errors))
    except Exception as e:  # noqa: BLE001
        errors.append(f"operator DM: {e}")
    log.error("routine failure with no agent and no DM: %s | %s", summary, errors)
    return Escalation(via="log", peer=peer, errors=tuple(errors))
