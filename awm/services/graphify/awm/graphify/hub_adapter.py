"""Hub adapter for the graphify service — a queryable knowledge graph of awm.

Boots graphify as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

On the collapsed MCP surface this service appears as a single ``graphify``
domain tool (``graphify(verb="build")``, ``verb="query"``, …); call
``graphify(verb="describe")`` to list verbs and their parameter schemas.
CLI and HTTP stay expanded as ``graphify_<verb>``.

Verbs:
  - build    (tool ``graphify_build``)    — build/refresh the graph; ~5s, incremental.
  - status   (tool ``graphify_status``)   — counts + staleness signal; never rebuilds.
  - query    (tool ``graphify_query``)    — BFS traversal for a natural-language question.
  - path     (tool ``graphify_path``)     — shortest path between two node labels.
  - explain  (tool ``graphify_explain``)  — plain-language explanation of a node.
  - affected (tool ``graphify_affected``) — transitive impact of changing a node.
  - find     (tool ``graphify_find``)     — search nodes by label substring.
  - refs     (tool ``graphify_refs``)     — callers / callees / importers of a node.

Read verbs (all except build/status) auto-rebuild when the graph is missing or
stale (AST-only, no LLM, no key — see :mod:`awm.graphify.runner`).

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
                "node/edge counts, build time, and staleness signal."
            ),
            "params": [_TARGET_PARAM],
            # extract is ~5s on the awm tree; allow generous headroom.
            "timeout": 600.0,
        },
        {
            "name": "status",
            "tool": "graphify_status",
            "description": (
                "Report whether a graph has been built for the target tree, with "
                "node/edge counts, the last build time, and a staleness signal "
                "(stale=true means source files have changed since the build). "
                "Never triggers a rebuild."
            ),
            "params": [_TARGET_PARAM],
        },
        {
            "name": "query",
            "tool": "graphify_query",
            "description": (
                "Ask a natural-language question about the codebase; runs a BFS "
                "traversal of the graph and returns structured nodes/edges "
                "(e.g. 'what connects the gateway to the scopes service'). "
                "Use context=['call'] to narrow the traversal and avoid "
                "truncation; budget sets the output token cap."
            ),
            "params": [
                {"name": "question", "type": "string", "required": True},
                {
                    "name": "context",
                    "type": "array",
                    "required": False,
                    "description": (
                        "Edge-type filter(s) to narrow the traversal — reduces "
                        "truncation. Values: call, import, type, field, "
                        "dependency, export, return_type, parameter_type."
                    ),
                },
                {
                    "name": "budget",
                    "type": "integer",
                    "required": False,
                    "description": "Output token cap (default: ~2000). Increase to get more results.",
                },
                _TARGET_PARAM,
            ],
            "timeout": 120.0,
        },
        {
            "name": "path",
            "tool": "graphify_path",
            "description": (
                "Find the shortest path between two node labels in the graph "
                "(e.g. 'serve' → 'create_session'). Use find first if the "
                "label is ambiguous."
            ),
            "params": [
                {"name": "a", "type": "string", "required": True},
                {"name": "b", "type": "string", "required": True},
                _TARGET_PARAM,
            ],
            "timeout": 120.0,
        },
        {
            "name": "explain",
            "tool": "graphify_explain",
            "description": (
                "Plain-language explanation of a node — its type, community, "
                "degree, source location, and immediate connections (callers, "
                "callees, imports). Use find first to resolve an ambiguous label."
            ),
            "params": [
                {
                    "name": "node",
                    "type": "string",
                    "required": True,
                    "description": "Exact node label (e.g. 'create_session').",
                },
                _TARGET_PARAM,
            ],
            "timeout": 60.0,
        },
        {
            "name": "affected",
            "tool": "graphify_affected",
            "description": (
                "Transitive impact of changing a node — reverse traversal to "
                "find all nodes that would break (callers of callers, importers, "
                "etc.). Use before a refactor to understand blast radius."
            ),
            "params": [
                {
                    "name": "node",
                    "type": "string",
                    "required": True,
                    "description": "Node label whose change impact to assess.",
                },
                {
                    "name": "depth",
                    "type": "integer",
                    "required": False,
                    "description": "Max traversal depth (default: 2).",
                },
                {
                    "name": "relations",
                    "type": "array",
                    "required": False,
                    "description": "Edge relation types to follow (default: calls, imports, references, …).",
                },
                _TARGET_PARAM,
            ],
            "timeout": 120.0,
        },
        {
            "name": "find",
            "tool": "graphify_find",
            "description": (
                "Search for nodes by label (case-insensitive substring). Returns "
                "ranked matches (exact first) with file, line, and type. Use "
                "this to disambiguate a label before calling path or explain."
            ),
            "params": [
                {
                    "name": "pattern",
                    "type": "string",
                    "required": True,
                    "description": "Substring to search for in node labels.",
                },
                {
                    "name": "limit",
                    "type": "integer",
                    "required": False,
                    "description": "Max results to return (default: 50).",
                },
                _TARGET_PARAM,
            ],
        },
        {
            "name": "refs",
            "tool": "graphify_refs",
            "description": (
                "Find callers, callees, and importers of a node. "
                "direction='in' returns what calls/imports this node; "
                "direction='out' returns what this node calls/uses; "
                "direction='both' (default) returns all. "
                "Ambiguous labels return refs for all matched nodes."
            ),
            "params": [
                {
                    "name": "node",
                    "type": "string",
                    "required": True,
                    "description": "Node label (substring match).",
                },
                {
                    "name": "direction",
                    "type": "string",
                    "required": False,
                    "description": "in | out | both (default: both).",
                },
                _TARGET_PARAM,
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


HANDLERS = {
    "build":    lambda a: runner.build(a.get("target")),
    "status":   lambda a: runner.status(a.get("target")),
    "query":    lambda a: runner.query(
                    a["question"],
                    context=a.get("context"),
                    budget=a.get("budget"),
                    target=a.get("target"),
                ),
    "path":     lambda a: runner.path(a["a"], a["b"], a.get("target")),
    "explain":  lambda a: runner.explain(a["node"], a.get("target")),
    "affected": lambda a: runner.affected(
                    a["node"],
                    depth=a.get("depth"),
                    relations=a.get("relations"),
                    target=a.get("target"),
                ),
    "find":     lambda a: runner.find(
                    a["pattern"],
                    limit=a.get("limit", 50),
                    target=a.get("target"),
                ),
    "refs":     lambda a: runner.refs(
                    a["node"],
                    direction=a.get("direction", "both"),
                    target=a.get("target"),
                ),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("graphify", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
