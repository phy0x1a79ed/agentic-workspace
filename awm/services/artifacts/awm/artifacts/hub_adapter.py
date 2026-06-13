"""Hub adapter for the artifacts service.

Boots the artifacts service as a gateway-registered process: stands up its own
DB (via ``dao.init``), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect). The artifact functions are exposed
over the control WS and projected into the gateway catalog.

Run via ``start.sh`` (which the hub spawns and respawns):
    python -m awm.artifacts.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.artifacts import dao
from awm.artifacts.artifacts import (
    register_artifact,
    delete_artifact,
    search_artifacts,
    get_content,
    sync_artifacts,
)
from awm.artifacts.models import ArtifactRegisterRequest

log = logging.getLogger("awm.artifacts.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "register",
            "tool": "artifact_register",
            "description": "Register or update an artifact (figure, dataset, report, model, script, other).",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "artifact_type", "type": "string", "required": True,
                 "description": "figure|dataset|report|model|script|other"},
                {"name": "path", "type": "string", "required": True,
                 "description": "Path relative to workspace root"},
                {"name": "description", "type": "string"},
                {"name": "format", "type": "string"},
                {"name": "tags", "type": "string"},
            ],
        },
        {
            "name": "search",
            "tool": "artifact_search",
            "description": "Search/list registered artifacts by project, type, or free-text query.",
            "params": [
                {"name": "project", "type": "string"},
                {"name": "scope", "type": "string"},
                {"name": "artifact_type", "type": "string"},
                {"name": "query", "type": "string"},
                {"name": "limit", "type": "integer"},
                {"name": "offset", "type": "integer"},
            ],
        },
        {
            "name": "delete",
            "tool": "artifact_delete",
            "description": "Delete an artifact by id.",
            "params": [
                {"name": "artifact_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "get",
            "tool": "artifact_get",
            "description": "Fetch artifact metadata by id.",
            "params": [
                {"name": "artifact_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "sync",
            "tool": "artifact_sync",
            "description": "Sync artifact status with on-disk reality; prune stale embeddings.",
            "params": [
                {"name": "force", "type": "boolean"},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _handle_register(args: dict) -> dict:
    req = ArtifactRegisterRequest(**args)
    info = register_artifact(req)
    return info.model_dump()


def _handle_search(args: dict) -> dict:
    result = search_artifacts(
        project=args.get("project"),
        scope=args.get("scope"),
        artifact_type=args.get("artifact_type"),
        query=args.get("query"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )
    return result.model_dump()


def _handle_delete(args: dict) -> dict:
    return delete_artifact(args["artifact_id"])


def _handle_get(args: dict) -> dict:
    from awm.artifacts.dao import ArtifactsDAO
    from awm.artifacts.artifacts import _row_to_info
    dao_obj = ArtifactsDAO()
    try:
        aid = int(str(args["artifact_id"]).split("@", 1)[0])
    except ValueError as exc:
        raise ValueError(f"Artifact {args['artifact_id']!r} not found") from exc
    row = dao_obj.get_by_id(aid)
    if row is None:
        raise ValueError(f"Artifact {aid} not found")
    return _row_to_info(row).model_dump()


def _handle_sync(args: dict) -> dict:
    return sync_artifacts(force=bool(args.get("force", False)))


HANDLERS = {
    "register": _handle_register,
    "search": _handle_search,
    "delete": _handle_delete,
    "get": _handle_get,
    "sync": _handle_sync,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "artifacts", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
