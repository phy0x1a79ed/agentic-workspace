"""Shared fixtures for the journal service tests."""

from __future__ import annotations

import pytest


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
def no_scopes_service(monkeypatch):
    """Every scope exists: no test reaches a gateway."""
    from awm.journal import owners
    owners._known.clear()
    monkeypatch.setattr(owners, "require_scope", lambda project, scope: None)


class FakeSearch:
    """Stands in for the search service's ``query`` over the journals source: it reads the
    post table as the journal exports it and ranks by how many query words a post holds."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status: dict = {"status": "ok"}
        self.error: Exception | None = None

    def __call__(self, service, fn, args=None, **kw):
        if (service, fn) != ("search", "query"):
            raise AssertionError(f"unexpected gateway call {service}.{fn}")
        self.calls.append(dict(args or {}))
        if self.error is not None:
            raise self.error
        import re
        from awm.journal.dao import JournalDAO
        from awm.journal.refs import iso_to_ms
        from awm.journal.search_index import INDEXED_KINDS
        words = set(re.findall(r"\w+", args["query"].lower()))
        sql, params = "SELECT * FROM scope_posts WHERE body != ''", []
        for key, col in (("project", "owner_project"), ("scope", "owner_scope")):
            if args.get(key):
                sql += f" AND {col} = ?"
                params.append(args[key])
        kinds = [k for k in (args.get("kind") or INDEXED_KINDS) if k in INDEXED_KINDS] or [""]
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
        if args.get("before"):
            sql += " AND ts < ?"
            params.append(iso_to_ms(args["before"]))
        exact, related = [], []
        for r in JournalDAO().query_all(sql, params):
            body = r["body"]
            score = len(words & set(re.findall(r"\w+", body.lower())))
            if not score:
                continue
            line = next((ln for ln in body.splitlines() if words & set(re.findall(r"\w+", ln.lower()))), body)
            hit = {"source": "journals", "id": r["id"], "score": float(score), "passages": [{"text": line[:300]}]}
            (exact if args["query"].lower() in body.lower() else related).append(hit)
        limit = int(args.get("limit") or 10)
        rank = lambda hits: sorted(hits, key=lambda h: -h["score"])[:limit]  # noqa: E731
        return {"exact": {"journals": rank(exact)}, "related": {"journals": rank(related)}, "graph": {},
                "sources": {"journals": self.status}}


@pytest.fixture(autouse=True)
def fake_search(monkeypatch):
    """Every ``journal fetch`` with a query reaches this stand-in, never a gateway."""
    import awm.gatewayclient as gc
    fake = FakeSearch()
    monkeypatch.setattr(gc, "call_sync", fake)
    return fake


@pytest.fixture()
def journal_workspace(tmp_path, monkeypatch):
    """A temporary workspace with the journal's database and the projects dir redirected."""
    workspace = tmp_path / "workspace"
    services_dir = workspace / ".awm" / "services"
    projects_dir = workspace / "projects"
    services_dir.mkdir(parents=True)
    projects_dir.mkdir()

    import awm.config as cfg
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", workspace / ".awm")
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)

    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)

    import awm.journal.dao as journal_dao
    monkeypatch.setattr(journal_dao, "_initialized", False)
    journal_dao.init()

    yield {"workspace": workspace, "services_dir": services_dir, "projects_dir": projects_dir}
    # Queued index writes resolve the DB path when they run: drain them while
    # it still points at this test's workspace.
    from awm.journal import search_index
    search_index.flush()


@pytest.fixture()
def journal_conn(journal_workspace):
    """A live connection to the journal DB for seeding rows."""
    from awm.persistence.databases import get_connection
    conn = get_connection("journal")
    yield conn
    conn.close()
