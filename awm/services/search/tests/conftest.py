"""Fixtures for the search service tests: a private database, a fake gateway, fake plug-ins and peers."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from awm.search.archive import Archive


@pytest.fixture(autouse=True)
def stub_embedder():
    """Index and search with a deterministic stub, never the real model."""
    from awm.persistence import embeddings
    from awm.persistence.search_testing import StubEmbedder
    emb = StubEmbedder()
    embeddings.use_embedder(emb)
    yield emb
    embeddings.use_embedder(None)


@pytest.fixture(autouse=True)
def node(monkeypatch, tmp_path):
    """A named node with an empty peer book, so no test reads the live one."""
    import awm.config as cfg
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path / ".awm")
    monkeypatch.setenv("AWM_NODE_NAME", "local")
    monkeypatch.setenv("AWM_SWARM", "tony")


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


class Exporter:
    """A plug-in's store behind the export contract: keys ascending, a keyset cursor, ids last."""

    def __init__(self, docs: dict[str, dict[str, Any]] | None = None) -> None:
        self.docs = docs or {}
        self.fail: Exception | None = None
        self.calls = 0

    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise self.fail
        size = max(1, min(int(args.get("limit") or 200), 1000))
        cursor = args.get("cursor")
        keys = sorted(self.docs)
        rest = [k for k in keys if not cursor or k > cursor]
        page, more = rest[:size], len(rest) > size
        out: dict[str, Any] = {"documents": [{"id": k, **self.docs[k]} for k in page],
                               "next_cursor": page[-1] if more else None, "done": not more}
        if not more:
            out["ids"] = keys
        return out


def doc(title: str, text: str, ts: str = "2026-03-01T10:00:00Z", **extra: Any) -> dict[str, Any]:
    return {"title": title, "text": text, "ts": ts, **extra}


def entry(service: str, tables: list[dict[str, Any]] | None = None, *, status: str = "ready",
          verbs: tuple[str, ...] = ("export",), archive: bool = True) -> dict[str, Any]:
    """One ``awm registry_list`` entry. A verb is published as ``<service>_<verb>``."""
    out = {"name": service, "kind": "service", "backend_status": status,
           "api": {"functions": [{"name": v, "tool": f"{service}_{v}"} for v in verbs]}}
    if archive:
        out["archive"] = tables or []
    return out


def index_table(name: str, service: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "mode": "index", "export": f"{service}_export", **extra}


class FakeGateway:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.handlers: dict[tuple[str, str], Callable[[dict], Any]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.queues: dict[tuple[str, str], asyncio.Queue] = {}

    def plug(self, name: str, docs: dict[str, dict[str, Any]], *, service: str | None = None,
             **table: Any) -> Exporter:
        """Register an index source backed by an :class:`Exporter` and return the exporter."""
        service = service or name
        exporter = Exporter(docs)
        self.entries = [e for e in self.entries if e["name"] != service]
        self.entries.append(entry(service, [index_table(name, service, **table)]))
        self.handlers[(service, "export")] = exporter
        return exporter

    def plug_live(self, name: str, hits: Callable[[dict], Any], *, service: str | None = None,
                  **table: Any) -> None:
        service = service or name
        self.entries.append(entry(service, [{"name": name, "mode": "live", "query": f"{service}_query",
                                             **table}], verbs=("query",)))
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
    """A peer book and one callable per peer standing in for its ``search_query``."""

    def __init__(self) -> None:
        self.book: dict[str, dict[str, Any]] = {}
        self.answers: dict[str, Callable[[dict], Any]] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, name: str, answer: Callable[[dict], Any], relation: str = "domestic",
            swarm: str = "tony") -> None:
        self.book[name] = {"name": name, "relation": relation, "swarm": swarm}
        self.answers[name] = answer

    def records(self) -> list[dict[str, Any]]:
        return list(self.book.values())

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
def peers() -> FakePeers:
    return FakePeers()


@pytest.fixture()
async def archive(search_db, gateway, peers, clock):
    arch = Archive(gateway, peers, now=clock)
    yield arch
    arch.close()


async def synced(arch: Archive, gateway: FakeGateway) -> Archive:
    """Read the registry and run one pass over every index source."""
    await arch.refresh()
    for name in arch._due_names():
        await arch.sweep(name)
    return arch
