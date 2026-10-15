"""The archive: the plug-in registry, the sweeps that fill the index, and the query over it.

An index source is paged through its service's ``export`` verb into the one
corpus, pushed on its topic when it has one and swept in full every hour. A
source whose service stops is frozen: its documents stay, because absence is
not deletion. A live source is asked at query time. The archive's sections,
each live source's and each peer's fuse by reciprocal rank, section by section.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from awm import config
from awm.gatewayclient import SupervisedSubscription
from awm.persistence import embeddings as engine

from awm.search import federation, hits as hitshape, index, merge, registry, store
from awm.search.federation import Peers
from awm.search.gateway import Gateway
from awm.search.query import Query
from awm.search.registry import Source

log = logging.getLogger("awm.search.archive")

SWEEP_EVERY_S = 3600.0
RETRY_S = 60.0
#: How long after a pushed event the sweep runs, so a burst of events costs one pass.
SETTLE_S = 5.0
TICK_S = 15.0
REGISTRY_TTL_S = 10.0
LIVE_TIMEOUT_S = 15.0
HOMES_TTL_S = 300.0
EXPORT_TIMEOUT_S = 120.0
PAGE = 200
MAX_PAGES = 100_000


def _describe(exc: BaseException) -> str:
    return (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:300]


def _with_conn(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    conn = store.connect()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def _drop_vanished(conn, source: str, kept: set[str]) -> int:
    return store.drop_documents(conn, source, store.document_ids(conn, source) - kept)


def _live_sections(source: str, raw: Any, origin: dict[str, str], limit: int) -> dict[str, list[dict[str, Any]]]:
    found = hitshape.sections(raw, origin, limit, source=source)
    for section in found.values():
        for hit in section:
            hit["ts"] = store.normalize_ts(hit["ts"])
    return found


class Archive:
    def __init__(self, gateway: Gateway | None = None, peers: Peers | None = None, *,
                 now: Callable[[], float] = time.time) -> None:
        self.gateway = gateway or Gateway()
        self.peers = peers or Peers()
        self.now = now
        self.sources: dict[str, Source] = {}
        self.registry_at: float | None = None
        self.registry_error: str | None = None
        self._due: dict[str, float] = {}
        self._sweeping: set[str] = set()
        self._redo: set[str] = set()
        self._wake = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-index")
        self._topics: dict[tuple[str, str], tuple[SupervisedSubscription, asyncio.Task]] = {}
        #: Source name → the node that is home to its domain, for a domain this node does not serve.
        self.homed: dict[str, str] = {}
        self.homed_at: float | None = None

    def origin(self) -> dict[str, str]:
        return {"node": config.node_name(), "swarm": config.node_swarm()}

    async def _index(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run an index write on the worker, so embedding never queues behind itself or a query."""
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: _with_conn(fn, *args, **kwargs))

    # -- registry ---------------------------------------------------------

    async def refresh(self) -> bool:
        """Read the registry; a failed read leaves everything as it was and says why."""
        try:
            entries = await self.gateway.services()
        except Exception as exc:  # noqa: BLE001 — the gateway being down is not a reason to forget sources
            self.registry_error = _describe(exc)
            return False
        sources, _ = registry.parse(entries)
        now = self.now()
        news = await asyncio.to_thread(_with_conn, store.record_registry,
                                       {n: s.spec() for n, s in sources.items()}, set(sources), now)
        stored = await asyncio.to_thread(_with_conn, store.source_rows)
        self.sources, self.registry_at, self.registry_error = sources, now, None
        for row in stored:
            source = sources.get(row["name"])
            if source and source.mode == "index" and not source.error:
                if row["name"] in news:
                    self._due[row["name"]] = now
                else:
                    self._due.setdefault(row["name"], (row["synced_at"] or 0) + SWEEP_EVERY_S)
        self._follow_topics()
        if news:
            self._wake.set()
        return True

    async def refresh_homes(self, force: bool = False) -> None:
        """Learn the sources of each domain declared homed on another node, at most every :data:`HOMES_TTL_S`."""
        if not force and self.homed_at is not None and self.now() - self.homed_at < HOMES_TTL_S:
            return
        served = {s.service for s in self.sources.values()}
        wanted = {d: h for d, h in federation.homes().items() if d not in served}
        found, unread = await federation.homed_sources(self.peers, wanted) if wanted else ({}, set())
        kept = {n: h for n, h in self.homed.items() if h in unread}
        self.homed, self.homed_at = {**kept, **found}, self.now()

    async def _refresh_if_stale(self) -> None:
        if self.registry_at is None or self.now() - self.registry_at > REGISTRY_TTL_S:
            await self.refresh()

    def _follow_topics(self) -> None:
        wanted = {(s.service, s.topic) for s in self.sources.values()
                  if s.mode == "index" and s.topic and not s.error}
        for key in wanted - self._topics.keys():
            service, topic = key
            sub = SupervisedSubscription(
                f"search/{service}.{topic}", lambda s=service, t=topic: self.gateway.events(s, t),
                lambda _event, k=key: self._pushed(k))
            self._topics[key] = (sub, asyncio.create_task(sub.run(), name=f"search:{service}.{topic}"))
        for key in self._topics.keys() - wanted:
            self._topics.pop(key)[1].cancel()

    def close(self) -> None:
        """Stop following topics and release the index worker."""
        for _, task in self._topics.values():
            task.cancel()
        self._topics.clear()
        self._pool.shutdown(wait=False)

    def _pushed(self, topic: tuple[str, str]) -> None:
        soon = self.now() + SETTLE_S
        for name, source in self.sources.items():
            if (source.service, source.topic) == topic and source.mode == "index":
                if name in self._sweeping:
                    self._redo.add(name)
                else:
                    self._due[name] = min(self._due.get(name, soon), soon)
        self._wake.set()

    # -- sweeps -----------------------------------------------------------

    async def _pass(self, source: Source) -> dict[str, int]:
        cursor, pages = None, 0
        totals = {"documents": 0, "changed": 0, "skipped": 0, "indexed": 0}
        while True:
            args: dict[str, Any] = {"limit": PAGE, **({"cursor": cursor} if cursor else {})}
            page = await self.gateway.call(source.service, source.export, args, EXPORT_TIMEOUT_S)
            if not (isinstance(page, dict) and isinstance(page.get("documents"), list)
                    and isinstance(page.get("done"), bool)):
                raise ValueError("export returned a page without documents and done")
            for key, value in (await self._index(index.apply_page, source.name, page["documents"])).items():
                totals[key] += value
            pages += 1
            if page["done"]:
                if not isinstance(page.get("ids"), list):
                    raise ValueError("the final export page carries no ids")
                dropped = await self._index(_drop_vanished, source.name, {str(i) for i in page["ids"]})
                return {**totals, "pages": pages, "dropped": dropped}
            following = page.get("next_cursor")
            if not following or following == cursor or pages >= MAX_PAGES:
                raise ValueError("the export cursor does not advance")
            cursor = following

    async def sweep(self, name: str) -> dict[str, Any]:
        """One full pass over an index source. A failure is recorded and retried, never raised."""
        source = self.sources.get(name)
        if source is None or source.mode != "index" or source.error:
            raise ValueError(f"{name!r} is not a running index source")
        if name in self._sweeping:
            return {"source": name, "skipped": "a pass is already running"}
        self._sweeping.add(name)
        self._redo.discard(name)
        try:
            result: dict[str, Any] = await self._pass(source)
            error = None
        except Exception as exc:  # noqa: BLE001 — recorded on the source, retried on the next tick
            result, error = {}, _describe(exc)
            log.warning("search: sweep of %s failed: %s", name, error)
        finally:
            self._sweeping.discard(name)
        now = self.now()
        await asyncio.to_thread(_with_conn, store.mark_attempt, name, error, now)
        self._due[name] = now + (RETRY_S if error else SETTLE_S if name in self._redo else SWEEP_EVERY_S)
        self._redo.discard(name)
        return {"source": name, **({"error": error} if error else result)}

    def _due_names(self) -> list[str]:
        now = self.now()
        return [n for n, s in self.sources.items()
                if s.mode == "index" and not s.error and n not in self._sweeping
                and self._due.get(n, now) <= now]

    async def run(self) -> None:
        """Refresh the registry and sweep what is due, forever; cancel to stop."""
        while True:
            self._wake.clear()
            try:
                await self.refresh()
                await self.refresh_homes()
                for name in self._due_names():
                    await self.sweep(name)
            except Exception:  # noqa: BLE001 — a bad tick must not end the loop
                log.exception("search: sync tick failed")
            soonest = min((self._due[n] for n in self.sources if n in self._due), default=None)
            wait = TICK_S if soonest is None else max(0.5, min(TICK_S, soonest - self.now()))
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def reindex(self, source: str | None = None, force: bool = False) -> dict[str, Any]:
        """Re-embed what is stale and queue a fresh pass over ``source`` (every index source if none)."""
        index_sources = [n for n, s in self.sources.items() if s.mode == "index" and not s.error]
        if source and source not in index_sources:
            raise ValueError(f"{source!r} is not a running index source")
        for name in [source] if source else index_sources:
            self._due[name] = self.now()
        self._wake.set()
        return {"engine": await self._index(index.reindex, force=force),
                "resync": [source] if source else index_sources}

    # -- reading ----------------------------------------------------------

    def _visible(self, rows: list[dict[str, Any]], grants: frozenset[str] | None) -> list[dict[str, Any]]:
        return [r for r in rows if grants is None or r["name"] in grants]

    async def sources_view(self, grants: frozenset[str] | None = None) -> dict[str, Any]:
        await self._refresh_if_stale()
        rows = self._visible(await asyncio.to_thread(_with_conn, store.source_rows), grants)
        out = []
        for r in rows:
            spec = r["spec"]
            entry = {"name": r["name"], "service": r["service"], "mode": r["mode"],
                     "count": r["count"] if r["mode"] == "index" else None,
                     "last_sync": store.iso(r["synced_at"]), "frozen": not r["present"],
                     "last_error": spec.get("error") or r["last_error"]}
            entry.update({k: spec[k] for k in ("topic", "optin") if spec.get(k)})
            out.append(entry)
        return {"sources": out}

    async def status_view(self, grants: frozenset[str] | None = None) -> dict[str, Any]:
        await self._refresh_if_stale()
        rows = self._visible(await asyncio.to_thread(_with_conn, store.source_rows), grants)
        counts = {r["name"]: r["count"] for r in rows if r["mode"] == "index"}
        out: dict[str, Any] = {"documents": sum(counts.values()), "sources": counts}
        if grants is None:
            out.update({
                "semantic": engine.probe(),
                "registry": {"refreshed": store.iso(self.registry_at), "error": self.registry_error},
                "sweeping": sorted(self._sweeping),
                "subscriptions": [sub.health() for sub, _ in self._topics.values()]})
        return out

    async def get(self, source: str, doc_id: str, grants: frozenset[str] | None = None) -> dict[str, Any]:
        rows = {r["name"]: r for r in self._visible(
            await asyncio.to_thread(_with_conn, store.source_rows), grants)}
        row = rows.get(source)
        if row is None:
            raise ValueError(f"no source {source!r}")
        if row["mode"] != "index":
            raise ValueError(f"{source!r} is a live source: it holds no stored copy to get")
        doc = await asyncio.to_thread(_with_conn, store.get_document, source, doc_id)
        if doc is None:
            raise ValueError(f"no document {source}:{doc_id}")
        return {"document": {**{k: doc[k] for k in ("title", "text", "project", "scope", "kind", "ts", "url")
                                if doc[k]}, "source": source, "id": doc_id, "frozen": not row["present"],
                             "origin": self.origin()}}

    # -- query ------------------------------------------------------------

    def _archive_hits(self, q: Query, names: list[str]) -> tuple[dict[str, list[dict[str, Any]]], Any]:
        found, degraded = _with_conn(index.search, q.text, q.filters(names), q.limit, q.passages)
        origin = self.origin()
        return {s: [{**h, "origin": origin} for h in hits] for s, hits in found.items()}, degraded

    async def _live(self, source: Source, q: Query) -> tuple[Source, dict[str, list[dict[str, Any]]], dict[str, Any]]:
        args = {"query": q.text, "limit": q.limit, **q.raw}
        try:
            raw = await asyncio.wait_for(
                self.gateway.call(source.service, source.query, args, LIVE_TIMEOUT_S),
                timeout=LIVE_TIMEOUT_S + 1.0)
            found = _live_sections(source.name, raw, self.origin(), q.limit)
        except (asyncio.TimeoutError, TimeoutError):
            return source, {}, {"status": "error", "error": f"timeout after {LIVE_TIMEOUT_S:g}s"}
        except Exception as exc:  # noqa: BLE001 — one source failing never fails the search
            return source, {}, {"status": "error", "error": _describe(exc)}
        return source, found, {"status": "ok", "hits": hitshape.count(found)}

    def _index_status(self, row: dict[str, Any], hits: int) -> dict[str, Any]:
        state = "frozen" if not row["present"] else "unsynced" if row["synced_at"] is None else "ok"
        status: dict[str, Any] = {"status": state, "hits": hits}
        if row["last_error"]:
            status["error"] = row["last_error"]
        return status

    async def query(self, q: Query) -> dict[str, Any]:
        """The ``exact``, ``related`` and ``graph`` sections over the picked sources and peers, plus a
        status for every source and peer asked."""
        await self._refresh_if_stale()
        rows = {r["name"]: r for r in self._visible(
            await asyncio.to_thread(_with_conn, store.source_rows), q.grants)}
        statuses: dict[str, Any] = {}
        if not q.local:
            rows = {}
        if q.sources is None:
            picked = [n for n, r in rows.items() if r["mode"] == "index" or not r["spec"].get("optin")]
        else:
            picked = [n for n in q.sources if n in rows]
            if q.local:
                statuses.update({n: {"status": "absent"} for n in q.sources if n not in rows})
        index_names, live = [], []
        for name in picked:
            row = rows[name]
            if row["spec"].get("error"):
                statuses[name] = {"status": "error", "error": row["spec"]["error"]}
            elif row["mode"] == "index":
                index_names.append(name)
            elif name in self.sources:
                live.append(self.sources[name])
            else:
                statuses[name] = {"status": "frozen", "hits": 0}
        homed: dict[str, list[str]] = {}
        if q.local and not q.relayed:
            for name in (q.sources if q.sources is not None else list(self.homed)):
                if name in self.homed and name not in rows and q.allows(name):
                    homed.setdefault(self.homed[name], []).append(name)
                    statuses.pop(name, None)

        jobs: list[Any] = [asyncio.to_thread(self._archive_hits, q, index_names)] if index_names else []
        jobs += [self._live(source, q) for source in live]
        jobs += [federation.ask_home(self.peers, home, names, q.forwarded(), q.limit)
                 for home, names in homed.items()]
        if q.fan_out:
            jobs.append(federation.ask(self.peers, q.peer_names, q.forwarded(), q.limit))
        results = await asyncio.gather(*jobs, return_exceptions=True)

        lists, degraded, peer_status = [], None, {}
        if index_names:
            found = results.pop(0)
            if isinstance(found, BaseException):
                statuses.update({n: {"status": "error", "error": _describe(found)} for n in index_names})
            else:
                sections, degraded = found
                lists.append(sections)
                every = [h for hits in sections.values() for h in hits]
                per_source = {n: sum(1 for h in every if h["source"] == n) for n in index_names}
                statuses.update({n: self._index_status(rows[n], per_source[n]) for n in index_names})
        for source in live:
            found = results.pop(0)
            if isinstance(found, BaseException):
                statuses[source.name] = {"status": "error", "error": _describe(found)}
            else:
                _, sections, status = found
                lists.append(sections)
                statuses[source.name] = status
        for home, names in homed.items():
            asked = results.pop(0)
            for name in names:
                if isinstance(asked, BaseException):
                    statuses[name] = {"status": "error", "error": _describe(asked), "via": home}
                elif asked.status.get("status") != "ok":
                    statuses[name] = {**asked.status, "via": home}
                else:
                    count = sum(1 for hits in asked.found.values() for h in hits if h["source"] == name)
                    statuses[name] = {"status": "ok", "hits": count, "via": home}
            if not isinstance(asked, BaseException):
                lists.append(asked.found)
        if q.fan_out:
            asked = results.pop(0)
            if isinstance(asked, BaseException):
                peer_status["*"] = {"status": "error", "error": _describe(asked)}
            else:
                for result in asked:
                    lists.append(result.found)
                    peer_status[result.name] = result.status

        out: dict[str, Any] = {"query": q.text, **merge.fuse_sections(lists, q.limit),
                               "sources": statuses, "peers": peer_status}
        if degraded:
            out["degraded"] = degraded
        return out
