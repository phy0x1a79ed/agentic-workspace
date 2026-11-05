"""The archive: the plug-in registry, the feed reads that fill each index source's tables, and the query over them.

An index source is read through its service's ``export`` change feed into its own LanceDB tables: a read
after a pushed event, and one every hour. Each read starts at the cursor the last one stored, so it carries
only what changed. A source whose service stops is frozen: its entries stay, because absence is not deletion.
A live source is asked at query time, and a source named on another node is asked of that node's search.
Each source answers apart, section by section.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from awm import config
from awm.config import edges, privacy
from awm.gatewayclient import SupervisedSubscription
from awm.persistence import embeddings as engine

from awm.search import api_source, federation, hits as hitshape, lance_query, lance_store, merge, registry, store
from awm.search import query as request
from awm.search.federation import Peers
from awm.search.gateway import Gateway
from awm.search.lance_store import Store, Tables
from awm.search.registry import Source

log = logging.getLogger("awm.search.archive")

SWEEP_EVERY_S = 3600.0
RETRY_S = 60.0
#: How long after a pushed event the read runs, so a burst of events costs one read.
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


def _embedder() -> engine.Embedder | None:
    try:
        return engine.get_embedder()
    except engine.EmbeddingsUnavailable:
        return None


def _covered(hidden: Sequence[dict] | None, project: str, scope: str) -> bool:
    """Whether a private mark covers a live hit by its project and scope. ``None`` covers any hit naming one."""
    if hidden is None:
        return bool(project or scope)
    name, named_scope = privacy.canonical(project, scope or None)
    for mark in hidden:
        if name != mark["project"]:
            continue
        marked = mark["scope"]
        if marked is None or (named_scope is not None
                              and (named_scope == marked or named_scope.startswith(marked + "/"))):
            return True
    return False


def _unseen(hit: dict[str, Any], hidden: Sequence[dict] | None) -> bool:
    """Whether a live hit falls under a private mark, by project and scope or by transcript slug."""
    project = hit.get("project", "")
    if _covered(hidden, project, hit.get("scope", "")):
        return True
    return bool(hidden) and bool(project) and privacy.hidden_slug(project)


def _declared(spec: dict[str, Any]) -> tuple[dict[str, registry.Field], tuple[str, ...]] | None:
    """A stored source's fields and hit, or ``None`` when it declares no content field to index."""
    try:
        fields, hit = registry.parse_fields(spec.get("table") or {})
    except ValueError:
        return None
    return (fields, hit) if any(f.cls == "content" for f in fields.values()) else None


def _declaration(fields: dict[str, registry.Field], hit: Sequence[str]) -> dict[str, Any]:
    """The fields as a manifest writes them, and the hit, for ``search sources``."""
    return {"fields": {n: {"class": f.cls, "nested": f.nested} if f.nested else f.cls for n, f in fields.items()},
            "hit": list(hit)}


def _check_page(page: Any) -> None:
    if not (isinstance(page, dict) and isinstance(page.get("changes"), list)
            and isinstance(page.get("done"), bool) and isinstance(page.get("epoch"), str)):
        raise ValueError("export returned a page without changes, done and epoch")


class Archive:
    def __init__(self, gateway: Gateway | None = None, peers: Peers | None = None, *,
                 lance: Store | None = None, now: Callable[[], float] = time.time) -> None:
        self.gateway = gateway or Gateway()
        self.peers = peers or Peers()
        self.now = now
        self._lance = lance
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
        self._table: str | None = None

    def origin(self) -> dict[str, str]:
        return {"node": config.node_name()}

    @property
    def lance(self) -> Store:
        if self._lance is None:
            self._lance = Store()
        return self._lance

    async def _work(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a store write on the one worker, so writes never interleave and never block a query."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

    def _held(self, name: str, spec: dict[str, Any]) -> Tables | None:
        """A stored index source's tables, or ``None`` when it has none or declares nothing to index."""
        declared = _declared(spec)
        if declared is None or not self.lance.has(name):
            return None
        return self.lance.tables(name, *declared)

    # -- registry ---------------------------------------------------------

    async def refresh(self) -> bool:
        """Read the registry; a failed read leaves everything as it was and says why."""
        try:
            entries = await self.gateway.services()
        except Exception as exc:  # noqa: BLE001 — the gateway being down is not a reason to forget sources
            self.registry_error = federation.describe_error(exc)
            return False
        sources, _ = registry.parse(entries)
        table = api_source.fingerprint(entries)
        table_changed, self._table = table != self._table, table
        now = self.now()
        news = await asyncio.to_thread(_with_conn, store.record_registry,
                                       {n: s.spec() for n, s in sources.items()}, set(sources), now)
        stored = await asyncio.to_thread(_with_conn, store.source_rows)
        self.sources, self.registry_at, self.registry_error = sources, now, None
        for row in stored:
            name, source = row["name"], sources.get(row["name"])
            if row["mode"] != "index" and row["layout"]:
                await self._work(self._forget, name)
            if source and source.mode == "index" and not source.error:
                if name in news:
                    self._due[name] = now
                else:
                    self._due.setdefault(name, (row["synced_at"] or 0) + SWEEP_EVERY_S)
        self._follow_topics()
        if table_changed and api_source.NAME in sources:
            self._soon([api_source.NAME])
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
        """Stop following topics and release the store worker."""
        for _, task in self._topics.values():
            task.cancel()
        self._topics.clear()
        self._pool.shutdown(wait=False)

    def _pushed(self, topic: tuple[str, str]) -> None:
        self._soon([n for n, s in self.sources.items() if (s.service, s.topic) == topic and s.mode == "index"])

    def _soon(self, names: list[str]) -> None:
        """Bring the next read of each source forward to a few seconds from now."""
        soon = self.now() + SETTLE_S
        for name in names:
            if name in self._sweeping:
                self._redo.add(name)
            else:
                self._due[name] = min(self._due.get(name, soon), soon)
        self._wake.set()

    # -- feed reads (on the worker) -----------------------------------------

    def _forget(self, name: str) -> None:
        """Drop a source's tables and cursor, so its next read starts from the beginning of its feed."""
        self.lance.drop(name)
        _with_conn(store.save_feed_state, name, None, None, None)

    def _apply(self, source: Source, changes: list[Any], embedder: engine.Embedder | None) -> dict[str, int]:
        """Write one feed page. An entry with no content counts as a deletion."""
        tables = self.lance.tables(source.name, source.fields, source.hit)
        named = [c for c in changes if isinstance(c, dict) and c.get("id") not in (None, "")]
        held = [c for c in named if isinstance(c.get("entry"), dict)
                and any(str(c["entry"].get(n) or "").strip() for n in tables.content)]
        rows = [{**c["entry"], "id": str(c["id"])} for c in held]
        gone = [str(c["id"]) for c in named if c not in held]
        tables.delete(gone)
        embedded = tables.upsert(rows, embedder)
        return {"upserted": len(rows), "deleted": len(gone), "embedded": embedded}

    def _maintain(self, source: Source) -> dict[str, Any]:
        return self.lance.tables(source.name, source.fields, source.hit).maintain()

    # -- sweeps -----------------------------------------------------------

    async def _pass(self, source: Source) -> dict[str, Any]:
        """Read a source's feed from its stored cursor to the head. The cursor advances only after a page is
        written, so a failed page is read again next time. A layout the tables were not built with, a changed
        epoch or a reset from the feed rebuilds the source from the start."""
        embedder = _embedder()
        want = lance_store.layout(source.fields, source.hit, embedder.name if embedder else "")
        state = await asyncio.to_thread(_with_conn, store.feed_state, source.name)
        cursor, epoch = state["cursor"], state["epoch"]
        totals: dict[str, Any] = {"pages": 0, "upserted": 0, "deleted": 0, "embedded": 0}
        if state["layout"] != want:
            await self._work(self._forget, source.name)
            cursor, epoch, totals["rebuilt"] = None, None, True
        while True:
            args: dict[str, Any] = {"limit": PAGE, **({"since": cursor} if cursor else {})}
            page = await self.gateway.call(source.tool, source.export, args, EXPORT_TIMEOUT_S)
            _check_page(page)
            if page.get("reset") or (epoch is not None and page["epoch"] != epoch):
                if cursor is None:
                    raise ValueError("the feed asks for a reset of a read from its start")
                await self._work(self._forget, source.name)
                cursor, epoch, totals["rebuilt"] = None, None, True
                continue
            epoch = page["epoch"]
            for key, value in (await self._work(self._apply, source, page["changes"], embedder)).items():
                totals[key] += value
            following = page.get("next")
            await asyncio.to_thread(_with_conn, store.save_feed_state, source.name, following, epoch, want)
            totals["pages"] += 1
            if page["done"]:
                break
            if not following or following == cursor or totals["pages"] >= MAX_PAGES:
                raise ValueError("the feed cursor does not advance")
            cursor = following
        if totals["upserted"] or totals["deleted"]:
            totals["merged"] = (await self._work(self._maintain, source))["merged"]
        return totals

    async def sweep(self, name: str) -> dict[str, Any]:
        """One feed read of an index source. A failure is recorded and retried, never raised."""
        source = self.sources.get(name)
        if source is None or source.mode != "index" or source.error:
            raise ValueError(f"{name!r} is not a running index source")
        if name in self._sweeping:
            return {"source": name, "skipped": "a read is already running"}
        self._sweeping.add(name)
        self._redo.discard(name)
        try:
            result: dict[str, Any] = await self._pass(source)
            error = None
        except Exception as exc:  # noqa: BLE001 — recorded on the source, retried on the next tick
            result, error = {}, federation.describe_error(exc)
            log.warning("search: feed read of %s failed: %s", name, error)
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

    def _warm(self) -> None:
        """One small query per index source, so the first real query finds the indexes cached."""
        embedder = _embedder()
        for name, source in self.sources.items():
            tables = self._held(name, source.spec()) if source.mode == "index" else None
            if tables is None:
                continue
            try:
                lance_query.answer(tables, embedder, lance_query.Ask("true", None, "warm up", False, None, 1))
            except Exception as exc:  # noqa: BLE001 — a failed warm-up leaves the load to the first query
                log.warning("search: warm-up of %s failed: %s", name, exc)

    async def run(self) -> None:
        """Refresh the registry and read what is due, forever; cancel to stop."""
        warmed = False
        while True:
            self._wake.clear()
            try:
                await self.refresh()
                if not warmed and self.registry_at is not None:
                    warmed = True
                    await asyncio.to_thread(self._warm)
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

    async def reindex(self, source: str | None = None) -> dict[str, Any]:
        """Rebuild ``source`` (every index source if none) from the start of its feed."""
        index_sources = [n for n, s in self.sources.items() if s.mode == "index" and not s.error]
        if source and source not in index_sources:
            raise ValueError(f"{source!r} is not a running index source")
        names = [source] if source else index_sources
        for name in names:
            await self._work(self._forget, name)
            self._due[name] = self.now()
        self._wake.set()
        return {"rebuild": names}

    # -- reading ----------------------------------------------------------

    async def _rows(self, hidden: Sequence[dict] | None) -> list[dict[str, Any]]:
        """The stored sources, each index source with the count of entries this caller may see."""
        rows = await asyncio.to_thread(_with_conn, store.source_rows)
        return await asyncio.to_thread(self._counted, rows, hidden)

    def _counted(self, rows: list[dict[str, Any]], hidden: Sequence[dict] | None) -> list[dict[str, Any]]:
        for r in rows:
            tables = self._held(r["name"], r["spec"]) if r["mode"] == "index" else None
            r["count"] = tables.count(lance_query.veil_sql(tables.fields, hidden)) if tables else 0
        return rows

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
            try:
                entry.update(_declaration(*registry.parse_fields(spec.get("table") or {})))
            except ValueError:
                pass
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

    async def get(self, source: str, doc_id: str, hidden: Sequence[dict] | None = (),
                  caller: str | None = None) -> dict[str, Any]:
        """One document. ``caller`` names the node that relayed the call."""
        await self._refresh_if_stale()
        if source == api_source.NAME and source in self.sources:
            described = await api_source.document(self.gateway, doc_id, caller)
            return {"document": {**described, "source": source, "id": doc_id, "frozen": False,
                                 "origin": self.origin()}}
        rows = {r["name"]: r for r in await asyncio.to_thread(_with_conn, store.source_rows)}
        row = rows.get(source)
        if row is None:
            raise ValueError(f"no source {source!r}")
        if row["mode"] != "index":
            return await self._get_live(source, doc_id)
        tables = await asyncio.to_thread(self._held, source, row["spec"])
        doc = None if tables is None else await asyncio.to_thread(
            lance_query.document, tables, doc_id, lance_query.veil_sql(tables.fields, hidden))
        if doc is None:
            raise ValueError(f"no document {source}:{doc_id}")
        kept = {k: v for k, v in doc.items() if k != "id" and v not in (None, "", [])}
        return {"document": {**kept, "source": source, "id": doc_id, "frozen": not row["present"],
                             "origin": self.origin()}}

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

    def _answer(self, tables: Tables, src: request.Source, req: request.Request) -> dict[str, Any]:
        text = req.text
        contains = text.contains if text else None
        ask = lance_query.Ask(
            cond=lance_query.where(tables.fields, src.filter, req.hidden),
            tree=lance_query.compile_tree(contains, tables.content) if contains is not None else None,
            about=text.about if text else None, under_or=bool(text and text.under_or),
            sort=src.sort, limit=req.max)
        return lance_query.answer(tables, _embedder(), ask)

    async def _indexed(self, src: request.Source, row: dict[str, Any], req: request.Request) -> dict[str, Any]:
        """One archived source's answer from its tables."""
        tables = await asyncio.to_thread(self._held, src.kind, row["spec"])
        if tables is None:
            return {"exact": [], "error": "not built yet: its first feed read has not written"}
        try:
            return await asyncio.to_thread(self._answer, tables, src, req)
        except lance_query.Unserved as exc:
            return {"exact": [], "error": f"query.{src.name}: {exc}"}

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

    async def _here(self, src: request.Source, rows: dict[str, dict[str, Any]], req: request.Request,
                    caller: str | None) -> dict[str, Any]:
        row = rows.get(src.kind)
        if src.kind == api_source.NAME:
            src = dataclasses.replace(src, filter=api_source.veiled(src.filter, caller))
        if row is None:
            return {"exact": [], "error": f"this node has no {src.kind} source"}
        if row["spec"].get("error"):
            return {"exact": [], "error": row["spec"]["error"]}
        if row["mode"] == "index":
            return await self._indexed(src, row, req)
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
        rows = {r["name"]: r for r in await asyncio.to_thread(_with_conn, store.source_rows)}
        clashed = await self.clashed() if req.remote else set()
        here = [self._here(src, rows, req, caller) for src in req.sources]
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
    fields = request.hit_fields(src.kind)
    exact = [h for h in (hitshape.clean(x, fields) for x in exact if not _unseen(x, req.hidden)) if h]
    related = [h for h in (hitshape.clean(x, fields) for x in related if not _unseen(x, req.hidden)) if h]
    for hit in (*exact, *related):
        if "ts" in hit:
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
