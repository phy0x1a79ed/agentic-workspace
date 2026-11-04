"""The archive: the plug-in registry, the sweeps that fill the index, and the query over it.

An index source is paged through its service's ``export`` verb into the one
corpus, pushed on its topic when it has one and swept in full every hour. A
source whose service stops is frozen: its documents stay, because absence is
not deletion. A live source is asked at query time, and a source named on another node is
asked of that node's search. Each source answers apart, section by section.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from awm import config
from awm.config import edges, privacy
from awm.gatewayclient import SupervisedSubscription
from awm.persistence import embeddings as engine

from awm.search import federation, hits as hitshape, index, meaning, merge, registry, store
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
#: How long the journal's open tag clashes are trusted before they are read again.
CLASHES_TTL_S = 60.0
#: A hop's link type -> the ``along`` a live source's hop verb takes.
ALONG = {"citations": "cites", "cited_by": "cited_by", "authors": "authors"}
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


def _unseen(hit: dict[str, Any], hidden: Sequence[dict] | None) -> bool:
    """Whether a live hit falls under a private mark, by project and scope or by transcript slug."""
    project = hit.get("project", "")
    if index.covered(hidden, project, hit.get("scope", "")):
        return True
    return bool(hidden) and bool(project) and privacy.hidden_slug(project)


def _live_sections(source: str, raw: Any, origin: dict[str, str], limit: int,
                   hidden: Sequence[dict] | None) -> dict[str, list[dict[str, Any]]]:
    found = hitshape.sections(raw, origin, limit, source=source)
    for section in found:
        found[section] = [h for h in found[section] if not _unseen(h, hidden)]
        for hit in found[section]:
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
        self._clashes: tuple[float, set[str]] | None = None

    def origin(self) -> dict[str, str]:
        return {"node": config.node_name()}

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
            page = await self.gateway.call(source.tool, source.export, args, EXPORT_TIMEOUT_S)
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

    async def _rows(self, hidden: Sequence[dict] | None) -> list[dict[str, Any]]:
        """The stored sources, counting only the documents this caller may see."""
        return await asyncio.to_thread(_with_conn, store.source_rows, index.veil(hidden))

    async def sources_view(self, hidden: Sequence[dict] | None = ()) -> dict[str, Any]:
        await self._refresh_if_stale()
        rows = await self._rows(hidden)
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

    async def status_view(self, hidden: Sequence[dict] | None = ()) -> dict[str, Any]:
        await self._refresh_if_stale()
        rows = await self._rows(hidden)
        counts = {r["name"]: r["count"] for r in rows if r["mode"] == "index"}
        return {"documents": sum(counts.values()), "sources": counts,
                "semantic": engine.probe(),
                "registry": {"refreshed": store.iso(self.registry_at), "error": self.registry_error},
                "sweeping": sorted(self._sweeping),
                "subscriptions": [sub.health() for sub, _ in self._topics.values()]}

    async def get(self, source: str, doc_id: str, hidden: Sequence[dict] | None = ()) -> dict[str, Any]:
        await self._refresh_if_stale()
        rows = {r["name"]: r for r in await self._rows(hidden)}
        row = rows.get(source)
        if row is None:
            raise ValueError(f"no source {source!r}")
        if row["mode"] != "index":
            return await self._get_live(source, doc_id)
        doc = await asyncio.to_thread(_with_conn, store.get_document, source, doc_id)
        if doc is None or index.covered(hidden, doc["project"], doc["scope"], doc["tags"]):
            raise ValueError(f"no document {source}:{doc_id}")
        kept = {k: doc[k] for k in ("title", "text", "project", "scope", "kind", "ts", "url") if doc[k]}
        tags = [t for t in doc["tags"].split("\n") if t]
        return {"document": {**kept, **({"tags": tags} if tags else {}), "source": source, "id": doc_id,
                             "frozen": not row["present"], "origin": self.origin()}}

    async def _get_live(self, name: str, doc_id: str) -> dict[str, Any]:
        source = self.sources.get(name)
        if source is None or not source.get:
            raise ValueError(f"{name!r} is a live source that reads no document whole")
        raw = await asyncio.wait_for(self.gateway.call(source.tool, source.get, {"id": doc_id}, LIVE_TIMEOUT_S),
                                     timeout=LIVE_TIMEOUT_S + 1.0)
        doc = raw.get("document") if isinstance(raw, dict) else None
        if not isinstance(doc, dict):
            raise ValueError(f"no document {name}:{doc_id}")
        kept = {k: doc[k] for k in ("title", "text", "kind", "ts", "url", "journal")
                if isinstance(doc.get(k), str) and doc[k]}
        if isinstance(doc.get("authors"), list):
            kept["authors"] = [str(a)[:200] for a in doc["authors"][:200]]
        return {"document": {**kept, "source": name, "id": doc_id, "frozen": False, "origin": self.origin()}}

    # -- query ------------------------------------------------------------

    def _per_source(self, fn: Callable[..., Any], q: Query, names: list[str], limit: int) -> list[Any]:
        """``fn(conn, text, filters, limit, ...)`` once for each source, so each has its own ``limit``."""
        conn = store.connect()
        try:
            return [fn(conn, q.text, q.filters([name]), limit) for name in names]
        finally:
            conn.close()

    async def _archive_hits(self, q: Query, names: list[str]) -> tuple[dict[str, list[dict[str, Any]]], Any]:
        """``exact`` from the archive's text indexes; ``related`` from kb's entry index fused with any-word BM25
        and the archive's own embeddings for the sources kb holds, and from the archive's own embeddings alone
        for the rest, or for all of them while kb cannot answer."""
        kb_names = meaning.covered(names) if any(s.service == "kb" for s in self.sources.values()) else []
        asked = asyncio.ensure_future(
            meaning.ask(self.gateway, q.text, kb_names, q.filters(kb_names), q.limit)) if kb_names else None
        origin = self.origin()
        try:
            exact = [h for hits in await asyncio.to_thread(self._per_source, index.exact, q, names, q.limit)
                     for h in hits]
            by_meaning: list[dict[str, Any]] = []
            rest = names
            if asked is not None:
                try:
                    entries = await asked
                    meant = await asyncio.to_thread(_with_conn, index.resolve, entries, q.filters(kb_names),
                                                    q.limit * 2)
                    worded = await asyncio.to_thread(self._per_source, index.words, q, kb_names, q.limit * 2)
                    own = await asyncio.to_thread(self._per_source, index.related, q, kb_names, q.limit * 2)
                    # Three lists: kb's entry index, any-word BM25 and the archive's own embeddings. The third
                    # raised real-ask recall@10 from 0.342 to 0.488 and lost no judged set (journal b73eb9b5).
                    # The released engine ranked by the archive's own embeddings alone, and journal recall must
                    # not fall below it (fleet.md R136 item 4): fusion reorders that list's head and adds below it.
                    by_meaning = []
                    for name, hits, (mine, _) in zip(kb_names, worded, own):
                        anchor = [{**m, "origin": origin} for m in mine]
                        lists = [[{**m, "origin": origin} for m in meant if m["source"] == name],
                                 [{**w, "origin": origin} for w in hits], anchor]
                        fused = merge.fuse(lists, sum(map(len, lists)))
                        by_meaning += merge.within(fused, anchor, q.limit)[:q.limit * 2]
                    rest = [n for n in names if n not in kb_names]
                except Exception as exc:  # noqa: BLE001 — the archive's own embeddings answer instead
                    log.warning("search: kb entry index unavailable, using the archive's embeddings: %s",
                                _describe(exc))
        finally:
            if asked is not None and not asked.done():
                asked.cancel()
        brute = await asyncio.to_thread(self._per_source, index.related, q, rest, q.limit * 2) if rest else []
        degraded = next((d for _, d in brute if d), None)
        related = by_meaning + [h for hits, _ in brute for h in hits]
        return {"exact": [{**h, "origin": origin} for h in exact],
                "related": [{**h, "origin": origin} for h in related]}, degraded

    async def _live(self, source: Source, q: Query) -> tuple[Source, dict[str, list[dict[str, Any]]], dict[str, Any]]:
        args = {"query": q.text, "limit": q.limit, **q.raw}
        try:
            raw = await asyncio.wait_for(
                self.gateway.call(source.tool, source.query, args, LIVE_TIMEOUT_S),
                timeout=LIVE_TIMEOUT_S + 1.0)
            found = _live_sections(source.name, raw, self.origin(), q.limit, q.hidden)
        except (asyncio.TimeoutError, TimeoutError):
            return source, {}, {"status": "error", "error": f"timeout after {LIVE_TIMEOUT_S:g}s"}
        except Exception as exc:  # noqa: BLE001 — one source failing never fails the search
            return source, {}, {"status": "error", "error": _describe(exc)}
        status: dict[str, Any] = {"status": "ok", "hits": hitshape.count(found)}
        if isinstance(raw, dict) and isinstance(raw.get("degraded"), dict):
            status["degraded"] = {str(k): str(v)[:300] for k, v in raw["degraded"].items()}
        return source, found, status

    def _index_status(self, row: dict[str, Any], hits: int) -> dict[str, Any]:
        state = "frozen" if not row["present"] else "unsynced" if row["synced_at"] is None else "ok"
        status: dict[str, Any] = {"status": state, "hits": hits}
        if row["last_error"]:
            status["error"] = row["last_error"]
        return status

    # -- tag clashes ------------------------------------------------------

    async def clashed(self) -> set[str]:
        """The nodes an open tag clash stands between this node and, as the journal records them. A journal
        that cannot answer leaves the last set in force, and none before the first read."""
        if self._clashes is not None and self.now() - self._clashes[0] < CLASHES_TTL_S:
            return self._clashes[1]
        try:
            raw = await asyncio.wait_for(self.gateway.call("journal", "tag_list", {}, LIVE_TIMEOUT_S),
                                         timeout=LIVE_TIMEOUT_S + 1.0)
        except Exception as exc:  # noqa: BLE001 — the journal being down is not a clash
            log.warning("search: tag clashes unread: %s", _describe(exc))
            return self._clashes[1] if self._clashes else set()
        here = edges.label(config.node_name())
        nodes: set[str] = set()
        for c in (raw or {}).get("clashes") or []:
            pair = {edges.label(str(c.get("older_node") or "")), edges.label(str(c.get("newer_node") or ""))}
            if here in pair and len(pair) == 2:
                nodes |= pair - {here}
        self._clashes = (self.now(), nodes)
        return nodes

    async def refuse_clashed(self, node: str | None) -> None:
        """Refuse a call to or from ``node`` while an open tag clash stands between it and this node."""
        if node and edges.label(node) in await self.clashed():
            raise PermissionError(f"an open tag clash stands between {node} and this node; "
                                  "searches between them wait until an agent renames the tag")

    # -- hop --------------------------------------------------------------

    @staticmethod
    def _seeds(q: Query, name: str, lists: list[dict[str, list[dict[str, Any]]]]) -> list[str]:
        """Where a hop on ``name`` starts: the ids it was given, else that source's hits for the words in
        the first section that holds any, best first. ``related`` ranks every document by meaning, so its hits
        seed a walk only when nothing matched the words."""
        if q.hop and q.hop.start:
            return q.hop.start
        here = edges.label(config.node_name())
        for section in hitshape.READ_ONCE:
            found = [h["id"] for f in lists for h in f.get(section, [])
                     if h["source"] == name and edges.label(h["origin"]["node"]) == here]
            if found:
                return list(dict.fromkeys(found))
        return []

    async def _walk(self, q: Query, name: str, seeds: list[str], live: Source | None) -> list[dict[str, Any]]:
        """The hits one ``q.hop`` from ``seeds`` in source ``name``, in the walk's order."""
        hop = q.hop
        if live is None:
            return await asyncio.to_thread(_with_conn, index.walk, name, seeds, q.filters([name]), hop.limit)
        args = {"ids": seeds, "along": ALONG[hop.via], "limit": hop.limit,
                **{k: q.raw[k] for k in ("after", "before") if k in q.raw}}
        raw = await asyncio.wait_for(self.gateway.call(live.tool, live.hop, args, LIVE_TIMEOUT_S),
                                     timeout=LIVE_TIMEOUT_S + 1.0)
        hits = raw.get("hits") if isinstance(raw, dict) else None
        if not isinstance(hits, list):
            raise ValueError("the hop answered without hits")
        origin = self.origin()
        return [{**h, "match": hop.via} for h in (hitshape.clean(x, origin, name) for x in hits)
                if h and not _unseen(h, q.hidden)][:hop.limit]

    async def _hops(self, q: Query, index_names: list[str], live: list[Source],
                    lists: list[dict[str, list[dict[str, Any]]]], statuses: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk ``q.hop`` from each named source whose links include its type; record each walk's status."""
        hop = q.hop
        walks: list[tuple[str, Source | None]]
        if hop.via == "reply_to":
            walks = [(n, None) for n in index_names]
        else:
            walks = [(s.name, s) for s in live if s.hop and hop.via in s.links]
        walked = {n for n, _ in walks}
        for name in [*index_names, *(s.name for s in live)]:
            if name not in walked and name in statuses:
                statuses[name]["hop"] = {"status": "none", "detail": f"no {hop.via} links"}
        if not walks:
            return []
        if any(not neg for neg, _ in q.tags) and hop.via != "reply_to":
            for name, _ in walks:
                statuses[name]["hop"] = {"status": "ok", "hits": 0}
            return []
        seeds = {name: self._seeds(q, name, lists) for name, _ in walks}
        results = await asyncio.gather(*(self._walk(q, n, seeds[n], s) for n, s in walks if seeds[n]),
                                       return_exceptions=True)
        out: list[dict[str, Any]] = []
        for name, _ in walks:
            if not seeds[name]:
                statuses[name]["hop"] = {"status": "ok", "hits": 0}
                continue
            got = results.pop(0)
            if isinstance(got, BaseException):
                statuses[name]["hop"] = {"status": "error", "error": _describe(got)}
                continue
            statuses[name]["hop"] = {"status": "ok", "hits": len(got)}
            out += [{**h, "origin": h.get("origin") or self.origin()} for h in got]
        return out

    # -- query ------------------------------------------------------------

    async def query(self, q: Query, caller: str | None = None) -> dict[str, Any]:
        """The ``exact`` and ``related`` sections, and ``hop`` when one is asked, over the named sources of
        each node, plus a status for every source asked. Each source keeps its own order. ``caller`` names
        the node that relayed the call."""
        await self.refuse_clashed(caller)
        await self._refresh_if_stale()
        rows = {r["name"]: r for r in await self._rows(q.hidden)}
        statuses: dict[str, Any] = {n: {"status": "absent"} for n in q.elsewhere}
        statuses.update({n: {"status": "absent"} for n in q.sources if n not in rows})
        index_names, live = [], []
        for name in (n for n in q.sources if n in rows):
            row = rows[name]
            if row["spec"].get("error"):
                statuses[name] = {"status": "error", "error": row["spec"]["error"]}
            elif row["mode"] == "index":
                index_names.append(name)
            elif name in self.sources:
                live.append(self.sources[name])
            else:
                statuses[name] = {"status": "frozen", "hits": 0}

        searched_index, searched_live = index_names, live
        if q.raw.get("author"):  # only papers have authors: the filter is one no archived document can meet
            statuses.update({n: {"status": "ok", "hits": 0} for n in index_names})
            searched_index = []
        if any(not neg for neg, _ in q.tags):  # a live source's documents carry no tags, so none can meet one
            statuses.update({s.name: {"status": "ok", "hits": 0} for s in live})
            searched_live = []
        if not q.text:
            statuses.update({n: {"status": "ok", "hits": 0} for n in index_names})
            statuses.update({s.name: {"status": "ok", "hits": 0} for s in live})
            searched_index, searched_live = [], []

        clashed = await self.clashed() if q.remote else set()
        jobs: list[Any] = [self._archive_hits(q, searched_index)] if searched_index else []
        jobs += [self._live(source, q) for source in searched_live]
        jobs += [federation.ask_home(self.peers, node, q.forwarded(names), q.limit, edges.label(node) in clashed)
                 for node, names in q.remote.items()]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        lists, degraded = [], None
        if searched_index:
            found = results.pop(0)
            if isinstance(found, BaseException):
                statuses.update({n: {"status": "error", "error": _describe(found)} for n in searched_index})
            else:
                sections, degraded = found
                lists.append(sections)
                every = [h for hits in sections.values() for h in hits]
                per_source = {n: sum(1 for h in every if h["source"] == n) for n in searched_index}
                statuses.update({n: self._index_status(rows[n], per_source[n]) for n in searched_index})
        for source in searched_live:
            found = results.pop(0)
            if isinstance(found, BaseException):
                statuses[source.name] = {"status": "error", "error": _describe(found)}
            else:
                _, sections, status = found
                lists.append(sections)
                statuses[source.name] = status
        for node, names in q.remote.items():
            asked = results.pop(0)
            for name in names:
                statuses[f"{name}@{node}"] = _remote_status(asked, name)
            if not isinstance(asked, BaseException):
                lists.append(asked.found)

        hopped = await self._hops(q, index_names, live, lists, statuses) if q.hop else []
        fused = merge.fuse_sections([*lists, {"hop": hopped}] if hopped else lists, q.limit, self.origin()["node"])
        out: dict[str, Any] = {"query": q.text, **fused, "sources": statuses}
        if not q.hop:
            out.pop("hop", None)
        if degraded:
            out["degraded"] = degraded
        return out


def _remote_status(asked: Any, name: str) -> dict[str, Any]:
    """One remote source's status: the node's own for it, or the node's when the node did not answer."""
    if isinstance(asked, BaseException):
        return {"status": "error", "error": _describe(asked)}
    if asked.status.get("status") != "ok":
        return dict(asked.status)
    theirs = (asked.status.get("sources") or {}).get(name)
    if not isinstance(theirs, dict):
        return {"status": "absent"}
    kept = {k: theirs[k] for k in ("status", "hits", "error") if isinstance(theirs.get(k), (str, int))}
    if isinstance(theirs.get("hop"), dict):
        kept["hop"] = {k: v for k, v in theirs["hop"].items() if isinstance(v, (str, int))}
    return kept or {"status": "absent"}
