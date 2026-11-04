"""Hub adapter for the ``auth`` service — the credential authority.

Boots on the shared ``awm.gatewayclient.ServiceAdapter`` loop (register → ready
→ serve → reconnect). Publishes the session and device-token verbs.

Verbs:
  - ``edge_material``   signing secret + device token digests for the edge.
  - ``status``          session lifetime and live device tokens (the gateway's
                        ``awm status`` embeds it).
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
            "name": "status",
            "tool": "auth_status",
            "effect": "read",
            "surfaces": _CLI_HTTP,
            "description": "Report the session lifetime and the number of live "
                           "device tokens.",
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
                           "avatar stage session (the WebSocket "
                           "/hub/session/avatar/stage).",
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
    # Opt into the settings page (session lifetime).
    "config": CONTRACT.manifest_fragment(),
}


HANDLERS: dict[str, Any] = {
    "edge_material": service.h_edge_material,
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
