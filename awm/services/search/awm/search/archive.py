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

from awm.search import compile, federation, hits as hitshape, index, meaning, merge, registry, store
from awm.search import query as request
from awm.search.federation import Peers
from awm.search.gateway import Gateway
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
EXPORT_TIMEOUT_S = 120.0
PAGE = 200
MAX_PAGES = 100_000


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
            self.registry_error = federation.describe_error(exc)
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
            result, error = {}, federation.describe_error(exc)
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
            log.warning("search: tag clashes unread: %s", federation.describe_error(exc))
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

    # -- query ------------------------------------------------------------

    async def _entries(self, about: str, kind: str, narrowed: bool, limit: int) -> list[dict[str, Any]] | None:
        """kb's entry-index matches for ``about``, or ``None`` when kb cannot answer."""
        try:
            return await meaning.ask(self.gateway, about, [kind], narrowed, limit)
        except Exception as exc:  # noqa: BLE001 — the archive's own embeddings answer instead
            log.warning("search: kb entry index unavailable, using the archive's embeddings: %s",
                        federation.describe_error(exc))
            return None

    async def _ranked(self, about: str, allowed: index.Allowed, entries: Any, limit: int) -> list[dict[str, Any]]:
        """The documents ``allowed`` holds, best first by ``about``.

        Three lists fuse when kb's entry index answers: its matches, any-word BM25 and the archive's own
        embeddings. The third raised real-ask recall@10 from 0.342 to 0.488 and lost no judged set (journal
        b73eb9b5). The released engine ranked by the archive's own embeddings alone, and journal recall must not
        fall below it (fleet.md R136 item 4): fusion reorders that list's head and adds below it.
        """
        own, degraded = await asyncio.to_thread(_with_conn, index.related, about, allowed, limit * 2)
        if degraded:
            log.info("search: ranking by words alone: %s", degraded)
        found = await entries if entries is not None else None
        if found is None:
            return own[:limit]
        meant = await asyncio.to_thread(_with_conn, index.resolve, found, allowed, limit * 2)
        worded = await asyncio.to_thread(_with_conn, index.words, about, allowed, limit * 2)
        lists = [meant, worded, own]
        return merge.within(merge.fuse(lists, sum(map(len, lists))), own, limit)[:limit]

    async def _indexed(self, src: request.Source, req: request.Request) -> dict[str, Any]:
        """One archived source's answer. With no ``$about`` it is one read, newest first, that stops at ``max``."""
        text = req.text
        contains = text.contains if text else None
        if text is None or text.about is None:
            rows = await asyncio.to_thread(_with_conn, compile.newest, src.kind, src.filter, contains,
                                           req.hidden, req.max)
            return {"exact": [{"id": r["doc_id"], "title": r["title"], "ts": r["ts"]} for r in rows]}
        kb_holds = meaning.covered([src.kind]) and any(s.service == "kb" for s in self.sources.values())
        entries = asyncio.ensure_future(self._entries(text.about, src.kind, bool(src.filter) or contains is not None,
                                                      req.max)) if kb_holds else None
        try:
            jobs = [self._ranked(text.about, compile.allowed(src.kind, src.filter, contains, False, req.hidden),
                                 entries, req.max)]
            if text.under_or:
                jobs.append(self._ranked(text.about,
                                         compile.allowed(src.kind, src.filter, contains, True, req.hidden),
                                         entries, req.max))
            found = await asyncio.gather(*jobs)
        finally:
            if entries is not None and not entries.done():
                entries.cancel()
        return {"exact": found[0], **({"related": found[1]} if text.under_or else {})}

    async def _live(self, plug: Source, src: request.Source, req: request.Request) -> dict[str, Any]:
        """One live source's answer, through the thin translation its service takes today."""
        try:
            args = live_args(src, req)
        except ValueError as exc:
            return {"exact": [], "error": str(exc)}
        try:
            raw = await asyncio.wait_for(self.gateway.call(plug.tool, plug.query, args, LIVE_TIMEOUT_S),
                                         timeout=LIVE_TIMEOUT_S + 1.0)
        except (asyncio.TimeoutError, TimeoutError):
            return {"exact": [], "error": f"timeout after {LIVE_TIMEOUT_S:g}s"}
        except Exception as exc:  # noqa: BLE001 — one source failing never fails the search
            return {"exact": [], "error": federation.describe_error(exc)}
        try:
            return live_answer(raw, req, src)
        except ValueError as exc:
            return {"exact": [], "error": str(exc)}

    async def _here(self, src: request.Source, rows: dict[str, dict[str, Any]], req: request.Request) -> dict[str, Any]:
        row = rows.get(src.kind)
        if row is None:
            return {"exact": [], "error": f"this node has no {src.kind} source"}
        if row["spec"].get("error"):
            return {"exact": [], "error": row["spec"]["error"]}
        if row["mode"] == "index":
            return await self._indexed(src, req)
        if src.kind in self.sources:
            return await self._live(self.sources[src.kind], src, req)
        return {"exact": [], "error": "frozen: its service is not running"}

    async def _there(self, node: str, sources: list[request.Source], req: request.Request,
                     clashed: set[str]) -> dict[str, dict[str, Any]]:
        asked = await federation.ask_home(self.peers, node, req.forwarded(node), req.max,
                                          edges.label(node) in clashed)
        if asked.status.get("status") != "ok":
            error = asked.status.get("error") or asked.status.get("status")
            return {s.name: {"exact": [], "error": str(error)} for s in sources}
        return {s.name: asked.found.get(s.kind) or {"exact": [], "error": "the node did not answer for it"}
                for s in sources}

    async def query(self, req: request.Request, caller: str | None = None) -> dict[str, Any]:
        """``{source: {exact, related?, error?}}`` for each source named, each answered apart.
        ``caller`` names the node that relayed the call."""
        await self.refuse_clashed(caller)
        await self._refresh_if_stale()
        rows = {r["name"]: r for r in await self._rows(req.hidden)}
        clashed = await self.clashed() if req.remote else set()
        here = [self._here(src, rows, req) for src in req.sources]
        there = [self._there(node, srcs, req, clashed) for node, srcs in req.remote.items()]
        results = await asyncio.gather(*here, *there, return_exceptions=True)
        out: dict[str, Any] = {name: {"exact": [], "error": "a relayed call answers for this node alone"}
                               for name in req.elsewhere}
        for src, got in zip(req.sources, results):
            out[src.name] = ({"exact": [], "error": federation.describe_error(got)}
                             if isinstance(got, BaseException) else got)
        for (node, srcs), got in zip(req.remote.items(), results[len(here):]):
            if isinstance(got, BaseException):
                got = {s.name: {"exact": [], "error": federation.describe_error(got)} for s in srcs}
            out.update(got)
        return out


def _unserved(src: request.Source, field: str, served: str) -> ValueError:
    return ValueError(f"query.{src.name}.{field}: not yet served on {src.kind}; served: {served}")


def _scalar(src: request.Source, field: str, value: Any, served: str) -> str:
    if isinstance(value, dict) and list(value) == ["$eq"]:
        value = value["$eq"]
    if not isinstance(value, str) or "*" in value:
        raise _unserved(src, field, served)
    return value


def live_args(src: request.Source, req: request.Request) -> dict[str, Any]:
    """The arguments a live source's ``query`` verb takes for ``src``: one string of text and the fields its
    service reads today. Raises for what it cannot express."""
    text = req.text
    if text is None:
        raise ValueError(f"query.{src.name}: {src.kind} needs text; it lists nothing by its fields alone yet")
    shorthand = text.under_or and isinstance(text.contains, request.Lit) and text.contains.text == text.about
    if text.about is not None and (text.contains is None or shorthand):
        words = text.about
    elif text.about is None and isinstance(text.contains, request.Lit):
        words = text.contains.text
    else:
        raise ValueError(f"query.text: {src.kind} serves a string, one $contains or one $about so far")
    args: dict[str, Any] = {"query": words, "limit": req.max}
    if src.kind == "transcripts":
        for field, value in src.filter.items():
            if field != "project":
                raise _unserved(src, field, "project")
            args["project"] = _scalar(src, field, value, "project")
    elif src.kind == "kb":
        served = "author, ts with $gte and $lt"
        for field, value in src.filter.items():
            if field == "author":
                args["author"] = _scalar(src, field, value, served)
            elif field == "ts" and isinstance(value, dict) and set(value) <= {"$gte", "$lt"}:
                args.update({{"$gte": "after", "$lt": "before"}[op]: v for op, v in value.items()})
            else:
                raise _unserved(src, field, served)
    elif src.filter:
        raise _unserved(src, next(iter(src.filter)), "nothing")
    return args


def live_answer(raw: Any, req: request.Request, src: request.Source) -> dict[str, Any]:
    """A live source's sections as this signature's: a lone ``$about`` ranks everything into ``exact``, the
    string form keeps the source's own two sections, and a ``$contains`` keeps its exact matches. A bare
    ``hits`` list is a text match, so it counts as ``exact``."""
    if not isinstance(raw, dict):
        raise ValueError("the answer is not an object")
    if isinstance(raw.get("hits"), list) and not any(k in raw for k in hitshape.SECTIONS):
        exact, related = raw["hits"], []
    else:
        exact, related = _flat(raw.get("exact")), _flat(raw.get("related"))
    exact = [h for h in (hitshape.clean(x) for x in exact if not _unseen(x, req.hidden)) if h]
    related = [h for h in (hitshape.clean(x) for x in related if not _unseen(x, req.hidden)) if h]
    for hit in (*exact, *related):
        hit["ts"] = store.normalize_ts(hit["ts"])
    text = req.text
    if text.about is not None and not text.under_or:
        return {"exact": merge.fuse([exact, related], req.max)}
    if text.about is not None:
        shown = {h["id"] for h in exact[:req.max]}
        return {"exact": exact[:req.max], "related": [h for h in related if h["id"] not in shown][:req.max]}
    return {"exact": exact[:req.max]}


def _flat(section: Any) -> list[Any]:
    """A section's hits as one list: a flat list as sent, or the per-source lists of an answer in turn."""
    if isinstance(section, dict):
        return [h for hits in section.values() if isinstance(hits, list) for h in hits]
    return section if isinstance(section, list) else []
