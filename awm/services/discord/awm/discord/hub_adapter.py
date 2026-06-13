"""Hub adapter for the discord service.

Boots the discord service as a gateway-registered process: stands up its own
DB, then runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop
(register → ready → serve → reconnect). The four operator functions are
exposed over the control WS and projected into the gateway catalog as
``discord_<fn>`` tools.

Run via ``start.sh`` (which the hub spawns and respawns):
    python -m awm.discord.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.discord import dao
from awm.discord.operators import (
    add_operator, list_operators, lookup, remove_operator,
)

log = logging.getLogger("awm.discord.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "list_operators",
            "description": "List whitelisted Discord operators.",
        },
        {
            "name": "add_operator",
            "description": "Whitelist a Discord user and map them to an awm_user.",
            "params": [
                {"name": "discord_user_id", "type": "string", "required": True},
                {"name": "awm_user", "type": "string", "required": True},
            ],
        },
        {
            "name": "remove_operator",
            "description": "Remove a Discord user from the whitelist.",
            "params": [
                {"name": "discord_user_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "lookup",
            "description": "Resolve a Discord user id to its awm_user (or null).",
            "params": [
                {"name": "discord_user_id", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "list_operators": lambda args: {"operators": list_operators()},
    "add_operator": lambda args: add_operator(
        args["discord_user_id"], args["awm_user"]),
    "remove_operator": lambda args: {
        "removed": remove_operator(args["discord_user_id"])},
    "lookup": lambda args: {"awm_user": lookup(args["discord_user_id"])},
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "discord", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
