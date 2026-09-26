from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.ssh.config import KNOWN_HOSTS
from awm.ssh.service import SSHService

log = logging.getLogger("awm.ssh.hub_adapter")

svc = SSHService()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "SSH to the managed HPC hosts, and the only sanctioned way to reach "
        "them. `connect` orchestrates the VPN and the Duo approver itself, so "
        "do NOT call the vpn or 2fa domains around it — they are already armed, "
        "once, for the single login it is about to make. After connecting, plain "
        "`ssh <host> <cmd>` multiplexes over the ControlMaster with no new "
        "authentication. A host reported 'unavailable' is held after an earlier "
        "failure and only an operator's `/approve <device>` in Discord releases "
        "it: retrying cannot, and each retry that reaches a login spends an MFA "
        "attempt against a 10-strike account lockout."
    ),
    "functions": [
        {
            "name": "connect",
            "tool": "ssh_connect",
            "description": (
                "Open a ControlMaster SSH connection to a managed host. "
                "Idempotent — if already connected it does an auth-free liveness "
                "check and returns without a new login. Orchestrates VPN + 2FA "
                "burst automatically for hosts that require them, deduplicating "
                "concurrent requests into a single attempt — you do not need to "
                "call the vpn or 2fa domains, and doing so spends MFA budget "
                "this service is accounting for. Blocks until the ControlMaster "
                "socket is live (up to 120s for cold VPN + 2FA), then returns "
                "success. Returns status 'unavailable' (no login attempted) if "
                "the host is held by the circuit breaker after a prior failure — "
                "recovery is an operator /approve in Discord, never a retry. An "
                "error saying no MFA attempt was spent is safe to retry. "
                "Known hosts: "
                + ", ".join(sorted(KNOWN_HOSTS))
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
            "timeout": 120,
        },
        {
            "name": "disconnect",
            "tool": "ssh_disconnect",
            "description": (
                "Close the ControlMaster SSH connection to a host. Does NOT abort "
                "an in-progress connect: if the host is mid-authentication the "
                "disconnect is deferred until that attempt resolves, then the "
                "connection is torn down. Waits for the control socket to be "
                "removed (reports a warning if the master may still be running)."
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "ssh_status",
            "description": (
                "List all managed hosts and their connection state. Reports an "
                "in-memory snapshot of each host's lifecycle state (plus a breaker "
                "lockfile check) — it does NOT probe the network. Per-host status "
                "is one of connected / connecting / disconnecting / disconnected, "
                "or 'unavailable' when the host is held by the circuit breaker. A "
                "host whose master died out-of-band may still read 'connected' "
                "until the next connect re-probes it."
            ),
            "params": [],
        },
        {
            "name": "notify_test",
            "tool": "ssh_notify_test",
            "description": (
                "Fire the Discord lock-alert wire on demand to confirm the ssh "
                "service can reach the operator BEFORE a real lockout depends on "
                "it. Sends through the exact social→Discord path a real breaker "
                "trip uses (same peer selector, edge, bearer, channel) but writes "
                "NO lockfile and mutates no state, and surfaces the send result "
                "instead of swallowing — so a broken notify wire fails loudly. The "
                "message is clearly a test and requests no /approve action."
            ),
            "params": [
                {"name": "host", "type": "string", "required": False},
            ],
        },
        {
            "name": "receive_test",
            "tool": "ssh_receive_test",
            "description": (
                "The inbound twin of notify_test: prove this service can still "
                "HEAR an operator /approve. Asks social to emit a synthetic, "
                "inert probe (a nonce only — no device, no channel, no Discord "
                "traffic) and waits for it to come back. Fails loudly if it does "
                "not, and distinguishes 'could not emit' from 'did not receive'. "
                "Opens no approval window, arms nothing, writes no lockfile."
            ),
            "params": [
                {"name": "timeout", "type": "number", "required": False},
            ],
        },
    ],
    "emitters": [],
    # Direct-session slot lease: a requester (this node or a peer) holds an open
    # WS for the duration of one connect attempt; the OPEN socket IS the lease.
    # This node is the fleet's slot arbiter for lockout-sensitive hosts. See
    # SSHService._lease_session and FEDERATION.md (SlotArbiter DFA).
    "sessions": [{"kind": "lease", "transport": "direct"}],
}


HANDLERS = {
    "connect": lambda args: svc.connect(args["host"]),
    "disconnect": lambda args: svc.disconnect(args["host"]),
    "status": lambda _args: svc.status(),
    "notify_test": lambda args: svc.notify_test(args.get("host", "[selftest]")),
    "receive_test": lambda args: svc.receive_test(
        float(args.get("timeout") or 10.0)),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "ssh", API_MANIFEST, HANDLERS,
        session_handlers={"lease": svc._lease_session},
        on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
