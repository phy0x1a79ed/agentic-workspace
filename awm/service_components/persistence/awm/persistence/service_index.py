"""A service's search index in the retrieval engine, written by one background worker.

One writer per service, so a write returns without waiting for its embedding,
the model is never loaded twice, and a reindex never races the backfill.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from awm.persistence import embeddings as engine
from awm.persistence.databases import get_connection, get_connection_at, service_db_path

log = logging.getLogger("awm.persistence.service_index")

BACKFILL_EVERY_S = 6 * 3600

#: Every source type the service indexes, each with the documents it holds now.
Documents = Callable[[sqlite3.Connection], dict[str, Iterable[engine.Document]]]


class ServiceIndex:
    def __init__(self, service: str, documents: Documents) -> None:
        self.service = service
        self._documents = documents
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{service}-index")

    def _run(self, db: Path, fn, *args) -> None:
        conn = get_connection_at(db)
        try:
            fn(conn, *args)
        except Exception:
            log.exception("%s search index update failed: %s%r", self.service, fn.__name__, args)
        finally:
            conn.close()

    def submit(self, fn: Callable[..., None], *args: Any) -> Future:
        """Queue ``fn(conn, *args)``. The job binds the DB now, not when it runs, so a write
        can never land in whatever database the process points at later."""
        return self.worker.submit(self._run, service_db_path(self.service), fn, *args)

    def flush(self) -> None:
        """Wait until every queued index update has been written."""
        self.worker.submit(lambda: None).result()

    def ensure_schema(self) -> None:
        conn = get_connection(self.service)
        try:
            engine.ensure_schema(conn)
        finally:
            conn.close()

    def reindex(self, *, force: bool = False, dry_run: bool = False,
                db: Path | None = None) -> dict[str, Any]:
        """Bring the index in line with the tables; returns counts per source type."""
        conn = get_connection_at(db or service_db_path(self.service))
        try:
            engine.ensure_schema(conn)
            return {st: engine.reindex(conn, st, docs, force=force, prune=True, dry_run=dry_run)
                    for st, docs in self._documents(conn).items()}
        finally:
            conn.close()

    def run_reindex(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """:meth:`reindex` on the worker, so it never races the backfill."""
        return self.worker.submit(self.reindex, force=force, dry_run=dry_run,
                                  db=service_db_path(self.service)).result()

    async def backfill_loop(self) -> None:
        """Reindex now and every :data:`BACKFILL_EVERY_S`; runs under ``spawn_supervised``."""
        loop = asyncio.get_running_loop()
        while True:
            counts = await loop.run_in_executor(self.worker, self.reindex)
            log.info("%s search backfill: %s", self.service,
                     {k: {x: v[x] for x in ("documents", "indexed", "pruned", "seconds")}
                      for k, v in counts.items()})
            await asyncio.sleep(BACKFILL_EVERY_S)

    def search(self, source_type: str, query: str, *, allowed: str | None = None,
               params: Iterable[Any] = (), limit: int = 10) -> engine.SearchResult:
        conn = get_connection(self.service)
        try:
            return engine.search(conn, query, source_type=source_type, allowed=allowed,
                                 params=params, limit=limit)
        finally:
            conn.close()
