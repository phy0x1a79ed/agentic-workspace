"""Hub adapter for the transcripts service — Claude Code session-log archival.

Boots transcripts as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve → reconnect).
The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

On the collapsed MCP surface this is a single ``transcripts`` domain tool
(``transcripts(verb="status")``, ``verb="sweep"``, …); CLI and HTTP stay
expanded as ``transcripts_<verb>`` (``awm transcripts sweep``).

It replaces ``~/.claude/scripts/archive-old-sessions.sh``, a loose script on one
machine driven by a systemd timer, whose only record of a run was a log line. Two
things it got wrong are why this exists: it walked by directory depth and so
orphaned every session's sidecar tree, and nothing ever pruned the archive.

``prune`` is the one verb that destroys data. It is CLI and HTTP only, never
reachable from the MCP surface, and it defaults to a dry run. An agent may
report on the archive and may sweep into it; deleting from it is a person's
decision.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.transcripts.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.transcripts import runs, search, status, sweep

log = logging.getLogger("awm.transcripts.hub_adapter")

_adapter: ServiceAdapter | None = None

# Deleting from the archive stays off the MCP surface: an agent reports on the
# archive, a human decides what leaves it.
_CLI_HTTP = ["cli", "http"]

_DAYS_PARAM = {
    "name": "days",
    "type": "number",
    "required": False,
    "description": "Retention in days. A session is aged when its newest file, "
                   "transcript or sidecar, is older than this. Default 7.",
}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "transcripts_status",
            "description": (
                "Report the live session tree and the archive: session count, "
                "orphaned sidecars, bytes, and how many sessions are past the "
                "retention and still unswept. past_retention is the backlog — a "
                "sweep that runs while that number grows is not working."
            ),
            "params": [_DAYS_PARAM, {
                "name": "prune_days", "type": "number", "required": False,
                "description": "Also report how much of the archive is older "
                               "than this many days.",
            }],
            "timeout": 120.0,
        },
        {
            "name": "sweep",
            "tool": "transcripts_sweep",
            "description": (
                "Gzip every session older than the retention into the archive "
                "and remove it from the live tree. The unit is the session, so "
                "a transcript and its sidecar directory move together. "
                "Reversible with restore."
            ),
            "params": [_DAYS_PARAM, {
                "name": "dry_run", "type": "boolean", "required": False,
                "description": "Report what would move, and move nothing.",
            }],
            "timeout": 900.0,
        },
        {
            "name": "search",
            "tool": "transcripts_search",
            "description": (
                "Regex-search archived transcripts without unpacking them. "
                "Returns at most 200 matching lines."
            ),
            "params": [
                {"name": "pattern", "type": "string", "required": True,
                 "description": "Python regular expression."},
                {"name": "project", "type": "string", "required": False,
                 "description": "Restrict to project directories containing "
                                "this substring."},
                {"name": "limit", "type": "number", "required": False,
                 "description": "Maximum hits to return. Default 200."},
            ],
            "timeout": 300.0,
        },
        {
            "name": "restore",
            "tool": "transcripts_restore",
            "description": (
                "Put one archived session back into the live tree, transcript "
                "and sidecar together. Refuses rather than overwrites when the "
                "live copy already exists."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True,
                 "description": "Project directory name, as status reports it."},
                {"name": "session", "type": "string", "required": True,
                 "description": "Session id."},
            ],
            "timeout": 300.0,
        },
        {
            "name": "runs",
            "tool": "transcripts_runs",
            "description": "Recent sweeps and prunes, newest first.",
            "params": [{"name": "limit", "type": "number", "required": False,
                        "description": "Rows to return. Default 20."}],
            "timeout": 60.0,
        },
        {
            "name": "prune",
            "tool": "transcripts_prune",
            "description": (
                "Delete archived sessions older than the retention. This is the "
                "only irreversible operation here, so it dry-runs unless "
                "dry_run is explicitly false."
            ),
            "params": [
                {"name": "days", "type": "number", "required": True,
                 "description": "Archive retention in days. There is no "
                                "default: deleting needs a number somebody "
                                "chose."},
                {"name": "dry_run", "type": "boolean", "required": False,
                 "description": "Defaults to true. Pass false to delete."},
            ],
            "surfaces": _CLI_HTTP,
            "timeout": 900.0,
        },
    ]
}


def _sweep(a: dict) -> dict:
    result = sweep.archive(days=float(a.get("days") or sweep.DEFAULT_RETENTION_DAYS),
                           dry_run=bool(a.get("dry_run")))
    result["run"] = runs.RunsDAO().record("sweep", result,
                                          trigger=a.get("_trigger", "manual"))
    return result


def _prune(a: dict) -> dict:
    result = sweep.prune(days=float(a["days"]),
                         dry_run=a.get("dry_run", True) is not False)
    result["run"] = runs.RunsDAO().record("prune", result,
                                          trigger=a.get("_trigger", "manual"))
    return result


HANDLERS = {
    "status":  lambda a: status.report(
                   days=float(a.get("days") or sweep.DEFAULT_RETENTION_DAYS),
                   prune_days=(float(a["prune_days"])
                               if a.get("prune_days") else None)),
    "sweep":   _sweep,
    "search":  lambda a: search.find(a["pattern"], project=a.get("project"),
                                     limit=int(a.get("limit") or search.MAX_HITS)),
    "restore": lambda a: sweep.restore(a["project"], a["session"]),
    "runs":    lambda a: {"runs": runs.RunsDAO().recent(int(a.get("limit") or 20))},
    "prune":   _prune,
}


async def _on_start() -> None:
    """Stand up the DB. Nothing that touches the filesystem tree belongs here —
    a service that stays unready gets killed by the gateway's orphan reaper."""
    await asyncio.to_thread(runs.init)


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _adapter = ServiceAdapter("transcripts", API_MANIFEST, HANDLERS,
                              on_start=_on_start)
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
