"""Hub adapter for the journal service.

Boots the journal as a gateway-registered process: stands up its own DB, copies
the post log over from the scopes database on first start, then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). It owns scope posts, subscribers and goals, publishes the ``posts``
topic, and renders ``history.md``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.journal.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised
from awm.journal import channel, dao, migrate, search_index
from awm.journal.operations.goals import GOAL_HANDLERS, GOAL_MANIFEST_FUNCTIONS
from awm.journal.operations.posts import POST_HANDLERS, POST_MANIFEST_FUNCTIONS
from awm.journal.operations.render import RENDER_HANDLERS, RENDER_MANIFEST_FUNCTIONS
from awm.persistence import embeddings

log = logging.getLogger("awm.journal.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": POST_MANIFEST_FUNCTIONS + GOAL_MANIFEST_FUNCTIONS + RENDER_MANIFEST_FUNCTIONS,
    "emitters": [
        {
            "topic": "posts",
            "description": (
                "Fires once per new scope-channel post. Payload "
                "{project, scope, post}; subscribers filter by (project, "
                "scope)."
            ),
        },
    ],
    "sessions": [],
}

HANDLERS: dict[str, Any] = {**POST_HANDLERS, **GOAL_HANDLERS, **RENDER_HANDLERS}


async def _on_start() -> None:
    dao.init()
    result = await asyncio.to_thread(migrate.migrate_from_scopes)
    log.info("scopes migration: %s", result)
    await asyncio.to_thread(search_index.ensure_schema)
    spawn_supervised("journal:search-backfill", search_index.backfill_loop)
    embeddings.warm()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    adapter = ServiceAdapter("journal", API_MANIFEST, HANDLERS, on_start=_on_start)
    # `channel.post()` runs in a worker thread, so the emitter hands the async
    # emit to this event loop thread-safely.
    loop = asyncio.get_running_loop()

    def _emit_posts(payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(adapter.emit("posts", payload), loop)

    channel.set_emitter(_emit_posts)
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
