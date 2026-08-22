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


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "password",
            "description": "Get the current day's awm login password and its "
                           "validity window (loopback/CLI only).",
            "params": [],
        },
        {
            "name": "peer_credential",
            "description": "Get the current peer credential and the path of the "
                           "file it is mirrored to for the SSH peer-auth channel.",
            "params": [],
        },
        {
            "name": "verify",
            "description": "Validate a login password; on success return a signed "
                           "session token for the edge to set as a cookie.",
            "params": [
                {"name": "password", "type": "string", "required": True},
            ],
        },
        {
            "name": "edge_material",
            "description": "Signing secret + currently-valid peer credentials + "
                           "session-lifetime knobs, for the httpsfront edge to "
                           "enforce auth offline.",
            "params": [],
        },
        {
            "name": "rotate",
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
