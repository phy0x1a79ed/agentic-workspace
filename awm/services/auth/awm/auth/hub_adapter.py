"""Hub adapter for the ``auth`` service — the credential authority.

Boots on the shared ``awm.gatewayclient.ServiceAdapter`` loop (register → ready
→ serve → reconnect). Publishes the credential + session verbs; the rotation
loop runs in ``on_start``.

Verbs:
  - ``password``        current login password + window (loopback/CLI).
  - ``verify``          shared login password → signed operator session token.
  - ``edge_material``   signing secret + device token digests for the edge.
  - ``rotate``          force a mint now (ops/testing).
  - ``status``          rotation state summary, incl. last push outcome (the
                        gateway's ``awm status`` embeds it).
  - ``device_mint`` / ``device_list`` / ``device_revoke``
                        revocable operator device tokens (CLI/HTTP,
                        operator only).
  - ``config_get``/``config_set`` from the config contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.auth import service
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.hub_adapter")


# Credential-bearing and admin verbs stay off the agent MCP surface.
_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "password",
            "tool": "auth_password",
            "effect": "secret",
            "surfaces": _CLI_HTTP,
            "description": "Get the current day's awm login password and its "
                           "validity window (loopback/CLI only).",
            "params": [],
        },
        {
            "name": "verify",
            "tool": "auth_verify",
            "effect": "secret",
            "surfaces": _CLI_HTTP,
            "description": "Validate the shared login password; on success "
                           "return a signed operator session token for the "
                           "edge to set as a cookie. A locked client IP "
                           "answers {ok:false, retry_after}.",
            "params": [
                {"name": "password", "type": "string", "required": True},
                {"name": "client_ip", "type": "string"},
            ],
        },
        {
            "name": "edge_material",
            "tool": "auth_edge_material",
            "effect": "secret",
            "surfaces": _CLI_HTTP,
            "description": "Signing secret + live device-token digests + session-lifetime "
                           "knobs, for the httpsfront edge to enforce auth "
                           "offline. device_used reports the device tokens "
                           "the edge accepted since its last fetch; "
                           "token_scopes names the scopes the edge enforces, "
                           "and a scoped token goes only to an edge that "
                           "names its scope.",
            "params": [{"name": "device_used", "type": "object"},
                       {"name": "token_scopes", "type": "array"}],
        },
        {
            "name": "rotate",
            "tool": "auth_rotate",
            "effect": "secret",
            "surfaces": _CLI_HTTP,
            "description": "Force minting a fresh credential pair now.",
            "params": [],
        },
        {
            "name": "status",
            "tool": "auth_status",
            "effect": "read",
            "surfaces": _CLI_HTTP,
            "description": "Report rotation state: valid generations, latest "
                           "window, cadence, the last Discord push attempt's "
                           "outcome, and the peer-cred file path.",
            "params": [],
        },
        {
            "name": "device_mint",
            "tool": "auth_device_mint",
            "effect": "secret",
            "surfaces": _CLI_HTTP,
            "description": "Mint a revocable operator device token, for an "
                           "unattended device or app that must not store a "
                           "password. The token is returned once. Operator "
                           "only. scope narrows the token to a few exact "
                           "request shapes: avatar-stage admits only the "
                           "avatar stage session (POST "
                           "/svc/avatar/session/stage and its WebSocket).",
            "params": [
                {"name": "label", "type": "string", "required": True},
                {"name": "scope", "type": "string"},
            ],
        },
        {
            "name": "device_list",
            "tool": "auth_device_list",
            "effect": "read",
            "surfaces": _CLI_HTTP,
            "description": "List device tokens (no secrets). Operator only.",
            "params": [],
        },
        {
            "name": "device_revoke",
            "tool": "auth_device_revoke",
            "effect": "write",
            "surfaces": _CLI_HTTP,
            "description": "Revoke a device token by id. The edge stops "
                           "honouring it within its ~30 s material refresh. "
                           "Operator only.",
            "params": [{"name": "id", "type": "string", "required": True}],
        },
    ],
    "emitters": [],
    "sessions": [],
    # Opt into the settings page (rotation cadence, validity, Discord push).
    "config": CONTRACT.manifest_fragment(),
}


HANDLERS: dict[str, Any] = {
    "password": service.h_password,
    "verify": service.h_verify,
    "edge_material": service.h_edge_material,
    "rotate": service.h_rotate,
    "status": service.h_status,
    "device_mint": service.h_device_mint,
    "device_list": service.h_device_list,
    "device_revoke": service.h_device_revoke,
    **CONTRACT.handlers(),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "auth",
        API_MANIFEST,
        HANDLERS,
        on_start=service.on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
