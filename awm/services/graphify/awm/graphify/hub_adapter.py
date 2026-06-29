"""Hub adapter for the graphify service — a queryable knowledge graph of awm.

Boots graphify as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

What the registration buys: a thin RPC surface over the ``graphify`` CLI,
projected into the gateway catalog as ``graphify_<fn>`` tools (MCP + CLI + HTTP).
The service indexes the awm source tree it runs in (AST-only, no LLM, no key —
see :mod:`awm.graphify.runner`) and answers structural questions about it:

  - build   (tool ``graphify_build``)  — build/refresh the graph; ~5s, incremental.
  - query   (tool ``graphify_query``)  — BFS traversal for a natural-language question.
  - path    (tool ``graphify_path``)   — shortest path between two node labels.
  - status  (tool ``graphify_status``) — graph presence + node/edge counts + build time.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.graphify.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.graphify import runner

log = logging.getLogger("awm.graphify.hub_adapter")

_TARGET_PARAM = {
    "name": "target",
    "type": "string",
    "required": False,
    "description": "Source tree to index (default: the awm tree this service runs in).",
}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "build",
            "tool": "graphify_build",
            "description": (
                "Build or refresh the knowledge graph of the awm source tree "
                "(AST-only, local, no API key). Incremental across runs. Returns "
                "node/edge counts and the build time."
            ),
            "params": [_TARGET_PARAM],
            # extract is ~5s on the awm tree; allow generous headroom so the
            # gateway RPC (30s default) never 504s mid-build.
            "timeout": 600.0,
        },
        {
            "name": "query",
            "tool": "graphify_query",
            "description": (
                "Ask a natural-language question about the codebase; runs a BFS "
                "traversal of the graph and returns the nodes/edges it reaches "
                "(e.g. 'what connects the gateway to the scopes service')."
            ),
            "params": [
                {"name": "question", "type": "string", "required": True},
                _TARGET_PARAM,
            ],
            "timeout": 120.0,
        },
        {
            "name": "path",
            "tool": "graphify_path",
            "description": (
                "Find the shortest path between two node labels in the graph "
                "(e.g. 'serve' → 'create_session')."
            ),
            "params": [
                {"name": "a", "type": "string", "required": True},
                {"name": "b", "type": "string", "required": True},
                _TARGET_PARAM,
            ],
            "timeout": 120.0,
        },
        {
            "name": "status",
            "tool": "graphify_status",
            "description": (
                "Report whether a graph has been built for the target tree, with "
                "node/edge counts and the last build time. Does not rebuild."
            ),
            "params": [_TARGET_PARAM],
        },
    ],
    "emitters": [],
    "sessions": [],
}


HANDLERS = {
    "build": lambda a: runner.build(a.get("target")),
    "query": lambda a: runner.query(a["question"], a.get("target")),
    "path": lambda a: runner.path(a["a"], a["b"], a.get("target")),
    "status": lambda a: runner.status(a.get("target")),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("graphify", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
