"""Hub adapter for the skills service.

Boots the skills service as a gateway-registered process: stands up its own
DB (``embeddings`` table), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect). The three public skills
operations (search, get, sync) are exposed over the control WS and projected
into the gateway catalog as ``skills_<fn>`` tools.

Run via ``start.sh`` (which the hub spawns and respawns):
    python -m awm.skills.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.skills import dao
from awm.skills.skills import get_skill, search_skills, sync_skills

log = logging.getLogger("awm.skills.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "search_skills",
            "description": "Search the skill catalog by free-text query and/or structural filters.",
            "params": [
                {"name": "query", "type": "string", "required": False},
                {"name": "type_filter", "type": "string", "required": False},
                {"name": "tags", "type": "array", "required": False},
            ],
        },
        {
            "name": "get_skill",
            "description": "Read a skill file by relative path (e.g. 'tools/git.md').",
            "params": [
                {"name": "path", "type": "string", "required": True},
            ],
        },
        {
            "name": "sync_skills",
            "description": "Sync the embeddings index with the live skills directory.",
            "params": [
                {"name": "force", "type": "boolean", "required": False},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "search_skills": lambda args: search_skills(
        query=args.get("query"),
        type_filter=args.get("type_filter"),
        tags=args.get("tags"),
    ).model_dump(),
    "get_skill": lambda args: get_skill(args["path"]).model_dump(),
    "sync_skills": lambda args: sync_skills(force=bool(args.get("force", False))),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "skills", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
