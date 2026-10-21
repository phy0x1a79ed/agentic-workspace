"""Private marks: what a peer caller never sees through query, get, sources and status."""

from __future__ import annotations

import pytest

from awm.config import privacy
from awm.search import hub_adapter
from awm.search.hub_adapter import HANDLERS

from .conftest import doc, every, flat, synced

pytestmark = [pytest.mark.search]

PEER, LOCAL, USER = "peer:altair", None, "user:tony"


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
async def served(archive, gateway, monkeypatch, root):
    gateway.plug("journals", {
        "j1": doc("Open", "kitchen sink in the open project", project="awm", scope="dev"),
        "j2": doc("Avatar", "kitchen sink in the private project", project="avatar", scope="dev"),
        "j3": doc("Task", "kitchen sink in a private task", project="task:avatar", scope="t1"),
        "j4": doc("User", "kitchen sink posted about the private project", scope="user:proj:avatar"),
        "j5": doc("Sibling", "kitchen sink in the sibling project", project="avatar2", scope="dev"),
        "j6": doc("Marked scope", "kitchen sink in a marked scope", project="awm", scope="svc-a"),
        "j7": doc("Nested scope", "kitchen sink in a nested marked scope", project="awm", scope="svc-a/x"),
        "j8": doc("Near scope", "kitchen sink in a scope that only starts alike", project="awm", scope="svc-ab"),
        "j9": doc("Goal", "kitchen sink goal set for the private project", scope="project:avatar"),
    })
    gateway.plug("precedence", {
        "p1": doc("Sink", "kitchen sink precedent with no project"),
        "p2": doc("Tagged", "kitchen sink precedent tagged with the private project", tags=["UI", "Avatar"]),
        "p3": doc("Near tag", "kitchen sink precedent tagged with a sibling", tags=["avatar2"]),
    })
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    privacy.write_marks([
        {"project": "avatar", "scope": None, "path": str(root / "avatar")},
        {"project": "awm", "scope": "svc-a", "path": str(root / "awm" / "svc-a")},
    ])
    return archive


HIDDEN = {"j2", "j3", "j4", "j6", "j7", "j9", "p2"}
ALL = {"j1", "j2", "j3", "j4", "j5", "j6", "j7", "j8", "j9", "p1", "p2", "p3"}


async def found(args, as_):
    return {h["id"] for h in every(await HANDLERS["query"]({"query": "kitchen sink", "limit": 50, **args}, as_))}


@pytest.mark.parametrize("as_", [LOCAL, USER, "agent:x"])
async def test_a_local_or_user_caller_sees_every_document(served, as_):
    assert await found({}, as_) == ALL


@pytest.mark.parametrize("as_", ["peer", PEER])
async def test_a_peer_never_sees_a_marked_project_or_scope(served, as_):
    assert await found({}, as_) == ALL - HIDDEN


async def test_a_peer_naming_a_marked_project_gets_what_an_unknown_one_gets(served):
    named = await HANDLERS["query"]({"query": "kitchen sink", "project": "avatar"}, PEER)
    unknown = await HANDLERS["query"]({"query": "kitchen sink", "project": "nonesuch"}, PEER)
    assert every(named) == every(unknown) == []
    assert named["sources"] == unknown["sources"]
    assert await found({"project": "awm", "scope": "svc-a"}, PEER) == set()


async def test_the_filter_runs_in_the_index_so_a_peer_page_stays_full(archive, gateway, monkeypatch, root):
    docs = {f"d{i:02}": doc(f"Doc {i}", f"kitchen sink number {i}", project="avatar" if i % 2 else "awm",
                            scope="dev") for i in range(20)}
    gateway.plug("journals", docs)
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(root / "avatar")}])
    out = await HANDLERS["query"]({"query": "kitchen sink", "limit": 5}, PEER)
    assert len(flat(out["exact"])) == 5 and {h["project"] for h in every(out)} == {"awm"}


async def test_get_answers_a_peer_as_if_a_hidden_document_did_not_exist(served):
    with pytest.raises(ValueError) as hidden:
        await HANDLERS["get"]({"source": "journals", "id": "j2"}, PEER)
    with pytest.raises(ValueError) as missing:
        await HANDLERS["get"]({"source": "journals", "id": "zz"}, PEER)
    assert str(hidden.value) == "no document journals:j2"
    assert str(missing.value) == "no document journals:zz"
    assert (await HANDLERS["get"]({"source": "journals", "id": "j2"}, LOCAL))["document"]["project"] == "avatar"
    assert (await HANDLERS["get"]({"source": "journals", "id": "j2"}, USER))["document"]["project"] == "avatar"
    assert (await HANDLERS["get"]({"source": "journals", "id": "j1"}, PEER))["document"]["project"] == "awm"


async def test_sources_and_status_count_only_what_a_peer_may_see(served):
    counts = {s["name"]: s["count"] for s in (await HANDLERS["sources"]({}, PEER))["sources"]}
    assert counts == {"journals": 3, "precedence": 2}
    status = await HANDLERS["status"]({}, PEER)
    assert status["sources"] == {"journals": 3, "precedence": 2} and status["documents"] == 5
    local = await HANDLERS["status"]({}, LOCAL)
    assert local["sources"] == {"journals": 9, "precedence": 3} and local["documents"] == 12
    assert (await HANDLERS["status"]({}, USER))["documents"] == 12


async def test_a_peer_never_sees_a_live_transcript_hit_from_a_marked_path(served, gateway, root):
    marked = privacy.slug(root / "avatar")
    sibling = privacy.slug(root / "avatar2")
    gateway.plug_live("transcripts", lambda args: {"hits": [
        {"id": "s1:1", "title": marked, "snippet": "kitchen sink", "project": marked},
        {"id": "s2:1", "title": marked + "-Avatar-dev", "snippet": "kitchen sink", "project": marked + "-Avatar-dev"},
        {"id": "s3:1", "title": sibling, "snippet": "kitchen sink", "project": sibling},
        {"id": "s4:1", "title": "avatar", "snippet": "kitchen sink", "project": "avatar", "scope": "dev"},
    ]}, optin=True)
    await served.refresh()
    args = {"query": "kitchen sink", "sources": ["transcripts"]}
    peer = await HANDLERS["query"](args, PEER)
    assert [h["id"] for h in every(peer)] == ["s3:1"]
    assert peer["sources"]["transcripts"] == {"status": "ok", "hits": 1}
    assert len(every(await HANDLERS["query"](args, LOCAL))) == 4


async def test_an_unreadable_marks_file_hides_every_project_from_a_peer(served, gateway):
    privacy.marks_path().write_text("{not json")
    assert await found({}, PEER) == {"p1"}
    assert await found({}, LOCAL) == ALL
    with pytest.raises(ValueError, match="no document journals:j1"):
        await HANDLERS["get"]({"source": "journals", "id": "j1"}, PEER)
    assert (await HANDLERS["status"]({}, PEER))["sources"] == {"journals": 0, "precedence": 1}
    gateway.plug_live("transcripts", lambda args: {"hits": [
        {"id": "s1:1", "title": "t", "snippet": "kitchen sink", "project": "-home-open"}]}, optin=True)
    await served.refresh()
    assert every(await HANDLERS["query"]({"query": "kitchen sink", "sources": ["transcripts"]}, PEER)) == []


async def test_a_peer_call_never_fans_out_further(served, peers):
    peers.add("shaula", lambda args: {"hits": []})
    out = await HANDLERS["query"]({"query": "kitchen sink", "peers": "fleet"}, PEER)
    assert out["peers"] == {} and peers.calls == []


async def test_get_hides_a_document_tagged_with_a_marked_project_from_a_peer(served):
    with pytest.raises(ValueError, match="^no document precedence:p2$"):
        await HANDLERS["get"]({"source": "precedence", "id": "p2"}, PEER)
    assert (await HANDLERS["get"]({"source": "precedence", "id": "p3"}, PEER))["document"]["title"] == "Near tag"
    assert (await HANDLERS["get"]({"source": "precedence", "id": "p2"}, LOCAL))["document"]["title"] == "Tagged"
    with pytest.raises(ValueError, match="^no document journals:j9$"):
        await HANDLERS["get"]({"source": "journals", "id": "j9"}, PEER)


async def test_unmarking_shows_a_tagged_document_again_without_a_resync(served):
    privacy.write_marks([])
    assert await found({}, PEER) == ALL


@pytest.mark.parametrize("project, scope", [
    ("avatar", ""), ("avatar", "dev"), ("task:avatar", "t1"), ("", "user:proj:avatar"),
    ("", "project:avatar"), ("", "project:avatar2"), ("avatar2", "dev"), ("", "user:proj:"),
    ("task:", ""), ("awm", "svc-a"), ("awm", "svc-a/x"), ("awm", "svc-ab"), ("task:awm", "svc-a"),
    ("", "project:awm"), ("", ""), ("project:avatar", ""),
])
def test_the_sql_veil_and_the_python_check_agree(search_db, root, project, scope):
    from awm.search import index, store
    marks = [{"project": "avatar", "scope": None, "path": str(root / "avatar")},
             {"project": "awm", "scope": "svc-a", "path": str(root / "awm" / "svc-a")}]
    where, params = index.veil(marks)
    conn = store.connect()
    try:
        kept = conn.execute(f"SELECT * FROM (SELECT ? AS project, ? AS scope, '' AS tags) WHERE {where}",
                            [project, scope, *params]).fetchall()
    finally:
        conn.close()
    assert index.covered(marks, project, scope) == (not kept)


def test_every_spelling_the_veil_lists_names_the_project_for_privacy():
    from awm.search import index
    for spelling in index.PROJECT_SPELLINGS:
        assert privacy.canonical(spelling.format("avatar"), None)[0] == "avatar"
    for spelling in index.CHANNEL_SPELLINGS:
        assert privacy.canonical("", spelling.format("avatar")) == ("avatar", None)


def test_a_version_1_index_gains_tags_and_resyncs_every_source(tmp_path, monkeypatch):
    import awm.persistence.databases as pdbs
    from awm.persistence.databases import get_connection, init_service_db
    from awm.search import store
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)
    v1 = store.SCHEMA_SQL.replace(",\n    tags     TEXT NOT NULL DEFAULT ''", "")
    assert "tags" not in v1.split("CREATE TABLE IF NOT EXISTS documents")[1].split(");")[0]
    init_service_db(store.SERVICE, v1, schema_version=1)
    conn = get_connection(store.SERVICE)
    conn.execute("INSERT INTO sources (name, service, mode, spec, synced_at, first_seen, last_seen)"
                 " VALUES ('precedence', 'precedence', 'index', '{}', 5, 1, 1)")
    conn.execute("INSERT INTO documents (key, source, doc_id, title) VALUES ('precedence:p1', 'precedence', 'p1', 't')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "_initialized", False)
    store.init()
    conn = store.connect()
    try:
        assert conn.execute("SELECT tags FROM documents").fetchone()[0] == ""
        assert conn.execute("SELECT synced_at FROM sources").fetchone()[0] is None
    finally:
        conn.close()
