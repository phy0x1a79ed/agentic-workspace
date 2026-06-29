"""Hub adapter for the vpn egress service.

Boots vpn as a gateway-registered process: stands up its own DB, then runs the
shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). The functions are projected into the gateway catalog as ``vpn_up`` /
``vpn_down`` / ``vpn_status``.

What it does: owns VPN exits as **singleton containers** (``awm-vpn-<profile>``,
profile ∈ {ubc, proton}). ``vpn_up`` brings one up (idempotent), waits for the
tunnel + an in-netns leak-check, and returns a self-documenting **descriptor**
telling a consumer exactly how to route a process — or ask rlm-browser for a
session — through that exit. The privileged dialing + fail-closed kill-switch
live inside the per-profile image (see ``containers/``); this process only
orchestrates docker (see ``container.py``).

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.vpn.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.vpn import container, dao

log = logging.getLogger("awm.vpn.hub_adapter")

# Set in main(); used by handlers to emit best-effort state-change events.
_adapter: ServiceAdapter | None = None

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "up",
            "tool": "vpn_up",
            "description": (
                "Bring a VPN exit up (profile: 'ubc' or 'proton'). Idempotent "
                "singleton — at most one awm-vpn-<profile> container at a time; "
                "a second call returns the existing exit. For UBC, arms the local "
                "awm 2fa Duo auto-approver (a burst on the configured device) "
                "right before dialing so the push auto-approves. Waits for the "
                "tunnel and an in-netns leak-check, then returns a descriptor: "
                "{profile, container, attach, network_mode, exit_ip, status, "
                "bounce_host, bounce_port, bounce_user, bounce_key, usage}. Route "
                "a process through the exit by netns attach (--network=container:"
                "awm-vpn-<profile>) OR by SSH ProxyJump/-W through the bounce sshd "
                "at localhost:bounce_port. Fails closed: if the tunnel never comes "
                "up the container is removed and an error is returned. Requires "
                "creds in $AWM_DIR/vpn.toml and a built image."
            ),
            "params": [
                {"name": "profile", "type": "string", "required": True},
            ],
        },
        {
            "name": "down",
            "tool": "vpn_down",
            "description": (
                "Tear a VPN exit down: stop+remove its container and clear its "
                "singleton record. Returns {profile, container, status, removed}."
            ),
            "params": [
                {"name": "profile", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "vpn_status",
            "description": (
                "Status of one exit (pass profile) or all exits (omit it), "
                "reconciled against docker. Returns {exits: [descriptor, ...]}. A "
                "container that died (tunnel dropped → kill-switch fired) reads "
                "as status 'down' even if it was previously up."
            ),
            "params": [
                {"name": "profile", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [
        {
            "topic": "state",
            "description": (
                "Fires on a VPN exit state change. Payload {profile, event, "
                "container, exit_ip} where event ∈ {up, down, degraded} — "
                "projected on /svc/vpn/emit/state. 'down' also fires when a live "
                "exit's tunnel drops (kill-switch), detected on the next status."
            ),
        },
    ],
    "sessions": [],
}


async def _emit_state(profile: str, event: str, *, container_name: str = "",
                      exit_ip: str = "") -> None:
    if _adapter is None:
        return
    await _adapter.emit("state", {
        "profile": profile, "event": event,
        "container": container_name or container.container_name(profile),
        "exit_ip": exit_ip,
    })


async def _up(args: dict) -> dict:
    profile = str(args["profile"]).strip()
    desc = await asyncio.to_thread(container.up, profile)
    await _emit_state(profile, desc["status"],
                      container_name=desc["container"], exit_ip=desc["exit_ip"])
    return desc


async def _down(args: dict) -> dict:
    profile = str(args["profile"]).strip()
    result = await asyncio.to_thread(container.down, profile)
    await _emit_state(profile, "down", container_name=result["container"])
    return result


async def _status(args: dict) -> dict:
    profile = args.get("profile")
    if profile:
        exits = [await asyncio.to_thread(container.status_one, str(profile).strip())]
    else:
        exits = await asyncio.to_thread(container.status_all)
    # Emit a down event for any exit that just crashed (kill-switch trip), then
    # strip the transient flag from the surfaced descriptors.
    for d in exits:
        if d.pop("just_down", False):
            await _emit_state(d["profile"], "down", container_name=d["container"])
    return {"exits": exits}


HANDLERS = {
    "up": _up,
    "down": _down,
    "status": _status,
}


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _adapter = ServiceAdapter(
        "vpn", API_MANIFEST, HANDLERS, on_start=dao.init,
    )
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
