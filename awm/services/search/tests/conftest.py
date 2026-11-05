"""Fixtures for the search service tests: a private database and store, a fake gateway, fake plug-ins and peers."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
import yaml

from awm.config import edges
from awm.search.archive import Archive

#: This node's name in every test's edge list.
HERE = "local"


@pytest.fixture(autouse=True)
def stub_embedder():
    """Index and search with a deterministic stub, never the real model."""
    from awm.persistence import embeddings
    from awm.persistence.search_testing import StubEmbedder
    emb = StubEmbedder(dim=384)
    embeddings.use_embedder(emb)
    yield emb
    embeddings.use_embedder(None)


@pytest.fixture(autouse=True)
def node(monkeypatch, tmp_path):
    """A named node with an empty peer book and edge list, so no test reads the live ones."""
    import awm.config as cfg
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path / ".awm")
    monkeypatch.setenv("AWM_NODE_NAME", HERE)
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(tmp_path / "edges.yml"))


@pytest.fixture()
def search_db(tmp_path, monkeypatch):
    """The search database redirected into a temporary services directory."""
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)
    from awm.search import store
    monkeypatch.setattr(store, "_initialized", False)
    store.init()
    return services_dir


#: The fields a test source declares unless it says otherwise: the journal's.
FIELDS = {"title": "content", "text": "content", "ts": "datetime", "project": "categorical",
          "scope": {"class": "categorical", "nested": "/"}, "tag": "categorical", "replies_to": "categorical"}
HIT = ["title", "ts"]


class _Docs(dict):
    """A feed's documents: each write or delete bumps the key on the feed, as the journal's triggers do."""

    def __init__(self, feed: "Feed") -> None:
        super().__init__()
        self.feed = feed

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        super().__setitem__(key, value)
        self.feed.bump(key)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.feed.bump(key)

    def pop(self, key: str, *default: Any) -> Any:
        out = super().pop(key, *default)
        self.feed.bump(key)
        return out


class Feed:
    """A plug-in's store behind the change-feed export contract: ``(seq, id)`` cursors, deletions without entry."""

    def __init__(self, docs: dict[str, dict[str, Any]] | None = None) -> None:
        self.seq: dict[str, int] = {}
        self.head = 0
        self.epoch = "epoch-1"
        self.docs = _Docs(self)
        for key, value in (docs or {}).items():
            self.docs[key] = value
        self.fail: Exception | None = None
        self.calls = 0

    def bump(self, key: str) -> None:
        self.head += 1
        self.seq[key] = self.head

    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise self.fail
        size = max(1, min(int(args.get("limit") or 200), 1000))
        since = args.get("since")
        seq, _, root = (since or "0:").partition(":")
        if int(seq) > self.head:
            return {"changes": [], "next": None, "done": True, "epoch": self.epoch, "reset": True}
        rest = sorted((q, k) for k, q in self.seq.items() if (q, k) > (int(seq), root))
        page, done = rest[:size], len(rest) <= size
        changes = [{"id": k, **({"entry": self.docs[k]} if k in self.docs else {})} for _, k in page]
        return {"changes": changes, "next": f"{page[-1][0]}:{page[-1][1]}" if page else since,
                "done": done, "epoch": self.epoch}


def every(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every hit of a query answer: source by source, each source's ``exact`` then its ``related``."""
    return [h for answer in result.values() for section in ("exact", "related") for h in answer.get(section, [])]


def section(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """One section's hits across every source of a query answer, source by source."""
    return [h for answer in result.values() for h in answer.get(name, [])]


def ids(hits: list[dict[str, Any]]) -> list[str]:
    return [h["id"] for h in hits]


def doc(title: str, text: str, ts: str = "2026-03-01T10:00:00Z", **extra: Any) -> dict[str, Any]:
    return {"title": title, "text": text, "ts": ts, **extra}


def entry(service: str, tables: list[dict[str, Any]] | None = None, *, status: str = "ready",
          tools: tuple[str, ...] | None = None, archive: bool = True) -> dict[str, Any]:
    """One ``awm services`` row. The service publishes the tool named after it unless ``tools`` says otherwise."""
    out = {"name": service, "plugin": service, "id": f"id-{service}", "status": status, "pid": 1,
           "verbs": 1, "refusal": None, "enabled": True, "tools": list(tools or (service,))}
    if archive:
        out["archive"] = tables or []
    return out


def index_table(name: str, service: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "mode": "index", "export": f"{service}_export", "fields": FIELDS, "hit": HIT, **extra}


class FakeGateway:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.handlers: dict[tuple[str, str], Callable[[dict], Any]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.queues: dict[tuple[str, str], asyncio.Queue] = {}

    def plug(self, name: str, docs: dict[str, dict[str, Any]], *, service: str | None = None,
             **table: Any) -> Feed:
        """Register an index source backed by a :class:`Feed` and return the feed."""
        service = service or name
        exporter = Feed(docs)
        self.entries = [e for e in self.entries if e["name"] != service]
        self.entries.append(entry(service, [index_table(name, service, **table)]))
        self.handlers[(service, "export")] = exporter
        return exporter

    def plug_live(self, name: str, hits: Callable[[dict], Any], *, service: str | None = None,
                  **table: Any) -> None:
        service = service or name
        self.entries.append(entry(service, [{"name": name, "mode": "live", "query": f"{service}_query",
                                             **table}]))
        self.handlers[(service, "query")] = hits

    def stop(self, service: str) -> None:
        self.entries = [e for e in self.entries if e["name"] != service]

    async def services(self) -> list[dict[str, Any]]:
        if self.error:
            raise self.error
        return list(self.entries)

    async def call(self, service: str, fn: str, args: dict[str, Any], timeout: float) -> Any:
        self.calls.append((service, fn, args))
        result = self.handlers[(service, fn)](args)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, Exception):
            raise result
        return result

    def events(self, service: str, topic: str):
        queue = self.queues.setdefault((service, topic), asyncio.Queue())

        async def stream():
            while True:
                yield await queue.get()
        return stream()

    def fire(self, service: str, topic: str) -> None:
        self.queues.setdefault((service, topic), asyncio.Queue()).put_nowait({"post": {}})


class FakePeers:
    """One callable per node standing in for its ``search_query``, and the edge list that reaches it.

    Each peer added also enters the edge list, with an edge from this node unless ``edge`` is false.
    """

    def __init__(self, edges_file: Any) -> None:
        self.edges_file = edges_file
        self.answers: dict[str, Callable[[dict], Any]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.fleet: dict[str, Any] = {"nodes": {HERE: {"role": "homebase"}}, "edges": {HERE: {}}}

    def add(self, name: str, answer: Callable[[dict], Any], *, edge: bool = True,
            role: str = "homebase") -> None:
        self.answers[name] = answer
        self.fleet["nodes"][name] = {"role": role}
        if edge:
            self.fleet["edges"][HERE][name] = "lan"
        self.edges_file.write_text(yaml.safe_dump(self.fleet))
        edges._load.cache_clear()

    def records(self) -> list[dict[str, Any]]:
        """The nodes this one holds an edge to, as the real :class:`Peers` lists them."""
        return [{"name": n} for n in edges.reachable_from(edges.here())]

    async def invoke(self, peer: str, name: str, args: dict[str, Any], timeout: float) -> Any:
        self.calls.append((peer, name, args))
        result = self.answers[peer](args)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, Exception):
            raise result
        return result


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture()
def peers(tmp_path) -> FakePeers:
    return FakePeers(tmp_path / "edges.yml")


@pytest.fixture()
async def archive(search_db, gateway, peers, clock):
    arch = Archive(gateway, peers, now=clock)
    yield arch
    arch.close()


async def synced(arch: Archive, gateway: FakeGateway) -> Archive:
    """Read the registry and run one feed read of every index source."""
    await arch.refresh()
    for name in arch._due_names():
        await arch.sweep(name)
    return arch
