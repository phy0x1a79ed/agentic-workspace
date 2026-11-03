"""Run the task store as a gateway service: ``python -m awm.taskqueue.hub_adapter``."""

from __future__ import annotations

import asyncio
import logging

from awm.gatewayclient import ServiceAdapter
from awm.taskqueue.api import API_MANIFEST, handlers, open_store


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    store = open_store()
    tasks = []

    async def on_start() -> None:
        from awm.taskqueue.writer import writer_from_env
        writer = writer_from_env(store)
        if writer is not None:
            tasks.append(asyncio.create_task(writer.run_forever()))

    await ServiceAdapter("taskqueue", API_MANIFEST, handlers(store), on_start=on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
