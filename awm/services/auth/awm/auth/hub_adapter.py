"""Hub adapter for the ``auth`` service — the credential authority.

Boots on the shared ``awm.gatewayclient.ServiceAdapter`` loop (register → ready
→ serve → reconnect). Publishes the credential + session verbs; the rotation
loop runs in ``on_start``.

Verbs:
  - ``password``        current login password + window (loopback/CLI).
  - ``peer_credential`` current peer credential + its mirror-file path.
  - ``verify``          login password → signed session token.
  - ``edge_material``   signing secret + valid peer creds for the edge.
  - ``rotate``          force a mint now (ops/testing).
  - ``status``          rotation state summary, incl. last push outcome.
  - ``user_add`` / ``user_passwd`` / ``user_disable`` / ``user_list``
                        static per-user accounts (CLI/HTTP only).
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
            "surfaces": _CLI_HTTP,
            "description": "Get the current day's awm login password and its "
                           "validity window (loopback/CLI only).",
            "params": [],
        },
        {
            "name": "peer_credential",
            "surfaces": _CLI_HTTP,
            "description": "Get the current peer credential and the path of the "
                           "file it is mirrored to for the SSH peer-auth channel.",
            "params": [],
        },
        {
            "name": "verify",
            "description": "Validate a login (per-user password when username "
                           "is given, else the shared password); on success "
                           "return a signed session token for the edge to set "
                           "as a cookie. A locked username/IP answers "
                           "{ok:false, retry_after}.",
            "params": [
                {"name": "password", "type": "string", "required": True},
                {"name": "username", "type": "string"},
                {"name": "client_ip", "type": "string"},
            ],
        },
        {
            "name": "edge_material",
            "surfaces": _CLI_HTTP,
            "description": "Signing secret + currently-valid peer credentials + "
                           "session-lifetime knobs, for the httpsfront edge to "
                           "enforce auth offline.",
            "params": [],
        },
        {
            "name": "rotate",
            "surfaces": _CLI_HTTP,
            "description": "Force minting a fresh credential pair now.",
            "params": [],
        },
        {
            "name": "status",
            "description": "Report rotation state: valid generations, latest "
                           "window, cadence, the last Discord push attempt's "
                           "outcome, and the peer-cred file path.",
            "params": [],
        },
        {
            "name": "user_add",
            "surfaces": _CLI_HTTP,
            "description": "Create a user account. The password is generated "
                           "server-side and returned once.",
            "params": [{"name": "username", "type": "string", "required": True}],
        },
        {
            "name": "user_passwd",
            "surfaces": _CLI_HTTP,
            "description": "Reset a user's password to a fresh generated one, "
                           "returned once. Clears the user's lockout.",
            "params": [{"name": "username", "type": "string", "required": True}],
        },
        {
            "name": "user_disable",
            "surfaces": _CLI_HTTP,
            "description": "Disable (default) or re-enable a user account.",
            "params": [
                {"name": "username", "type": "string", "required": True},
                {"name": "disabled", "type": "boolean"},
            ],
        },
        {
            "name": "user_list",
            "surfaces": _CLI_HTTP,
            "description": "List user accounts (no secrets).",
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
    # Opt into the settings page (rotation cadence, validity, Discord push).
    "config": CONTRACT.manifest_fragment(),
}


HANDLERS: dict[str, Any] = {
    "password": service.h_password,
    "peer_credential": service.h_peer_credential,
    "verify": service.h_verify,
    "edge_material": service.h_edge_material,
    "rotate": service.h_rotate,
    "status": service.h_status,
    "user_add": service.h_user_add,
    "user_passwd": service.h_user_passwd,
    "user_disable": service.h_user_disable,
    "user_list": service.h_user_list,
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
