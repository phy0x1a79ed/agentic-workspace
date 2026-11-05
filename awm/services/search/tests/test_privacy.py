"""Private marks: what a peer caller never sees through query, get, sources and status."""

from __future__ import annotations

import pytest

from awm.config import privacy
from awm.search import hub_adapter, store
from awm.search.hub_adapter import HANDLERS

from .conftest import doc, every, synced

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
        "j5": doc("Sibling", "kitchen sink in the sibling project", project="avatar2", scope="dev"),
        "j6": doc("Marked scope", "kitchen sink in a marked scope", project="awm", scope="svc-a"),
        "j7": doc("Nested scope", "kitchen sink in a nested marked scope", project="awm", scope="svc-a/x"),
        "j8": doc("Near scope", "kitchen sink in a scope that only starts alike", project="awm", scope="svc-ab"),
        "j9": doc("Goal", "kitchen sink goal set on the private project's channel", project="avatar"),
        "p1": doc("Sink", "kitchen sink precedent with no project"),
        "p2": doc("Tagged", "kitchen sink precedent tagged with the private project", tag=["UI", "Avatar"]),
        "p3": doc("Near tag", "kitchen sink precedent tagged with a sibling", tag=["avatar2"]),
    })
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    privacy.write_marks([
        {"project": "avatar", "scope": None, "path": str(root / "avatar")},
        {"project": "awm", "scope": "svc-a", "path": str(root / "awm" / "svc-a")},
    ])
    return archive


HIDDEN = {"j2", "j6", "j7", "j9", "p2"}
ALL = {"j1", "j2", "j5", "j6", "j7", "j8", "j9", "p1", "p2", "p3"}


async def ask(text, as_, **source):
    """The answer to a query for ``text`` over journals narrowed by ``source``, as ``as_``."""
    return await HANDLERS["query"]({"query": {"text": text, "journals": source}, "max": 50}, as_)


async def found(as_, text="kitchen sink", **source):
    return {h["id"] for h in every(await ask(text, as_, **source))}


@pytest.mark.parametrize("as_", [LOCAL, USER, "agent:x"])
async def test_a_local_or_user_caller_sees_every_document(served, as_):
    assert await found(as_) == ALL


@pytest.mark.parametrize("as_", ["peer", PEER])
async def test_a_peer_never_sees_a_marked_project_or_scope(served, as_):
    assert await found(as_) == ALL - HIDDEN


async def test_a_peer_never_sees_a_marked_document_in_either_section(served):
    out = await ask("kitchen sink", PEER)
    assert out["journals"].keys() == {"exact", "related"}
    assert not {h["id"] for h in every(out)} & HIDDEN
    listed = await ask({"$contains": "kitchen"}, PEER)
    assert {h["id"] for h in listed["journals"]["exact"]} == ALL - HIDDEN


async def test_a_peer_naming_a_marked_project_gets_what_an_unknown_one_gets(served):
    named = await ask("kitchen sink", PEER, project="avatar")
    unknown = await ask("kitchen sink", PEER, project="nonesuch")
    assert every(named) == every(unknown) == []
    assert named == unknown
    assert await found(PEER, project="awm", scope="svc-a") == set()


async def test_the_filter_runs_in_the_index_so_a_peer_page_stays_full(archive, gateway, monkeypatch, root):
    docs = {f"d{i:02}": doc(f"Doc {i}", f"kitchen sink number {i}", project="avatar" if i % 2 else "awm",
                            scope="dev") for i in range(20)}
    gateway.plug("journals", docs)
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(root / "avatar")}])
    out = await HANDLERS["query"]({"query": {"text": {"$contains": "kitchen sink"}, "journals": {}}, "max": 5}, PEER)
    shown = out["journals"]["exact"]
    assert len(shown) == 5 and all(int(h["id"][1:]) % 2 == 0 for h in shown)


async def test_get_answers_a_peer_as_if_a_hidden_document_did_not_exist(served):
    with pytest.raises(ValueError) as hidden:
        await HANDLERS["get"]({"source": "journals", "id": "j2"}, PEER)
    with pytest.raises(ValueError) as missing:
        await HANDLERS["get"]({"source": "journals", "id": "zz"}, PEER)
    assert str(hidden.value) == "no document journals:j2"
    assert str(missing.value) == "no document journals:zz"
    assert (await HANDLERS["get"]({"source": "journals", "id": "j2"}, LOCAL))["document"]["project"] == ["avatar"]
    assert (await HANDLERS["get"]({"source": "journals", "id": "j2"}, USER))["document"]["project"] == ["avatar"]
    assert (await HANDLERS["get"]({"source": "journals", "id": "j1"}, PEER))["document"]["project"] == ["awm"]


async def test_sources_and_status_count_only_what_a_peer_may_see(served):
    counts = {s["name"]: s["count"] for s in (await HANDLERS["sources"]({}, PEER))["sources"]}
    assert counts == {"journals": 5}
    status = await HANDLERS["status"]({}, PEER)
    assert status["sources"] == {"journals": 5} and status["documents"] == 5
    local = await HANDLERS["status"]({}, LOCAL)
    assert local["sources"] == {"journals": 10} and local["documents"] == 10
    assert (await HANDLERS["status"]({}, USER))["documents"] == 10


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
    query = {"query": {"text": "kitchen sink", "transcripts": {}}}
    peer = await HANDLERS["query"](query, PEER)
    assert [h["id"] for h in every(peer)] == ["s3:1"]
    assert len(every(await HANDLERS["query"](query, LOCAL))) == 4


async def test_an_unreadable_marks_file_hides_every_project_from_a_peer(served, gateway):
    privacy.marks_path().write_text("{not json")
    assert await found(PEER) == {"p1"}
    assert await found(LOCAL) == ALL
    with pytest.raises(ValueError, match="no document journals:j1"):
        await HANDLERS["get"]({"source": "journals", "id": "j1"}, PEER)
    assert (await HANDLERS["status"]({}, PEER))["sources"] == {"journals": 1}
    gateway.plug_live("transcripts", lambda args: {"hits": [
        {"id": "s1:1", "title": "t", "snippet": "kitchen sink", "project": "-home-open"}]}, optin=True)
    await served.refresh()
    assert every(await HANDLERS["query"]({"query": {"text": "kitchen sink", "transcripts": {}}}, PEER)) == []


async def test_a_peer_call_never_asks_another_node(served, peers):
    peers.add("shaula", lambda args: {"journals": {"exact": [], "related": []}})
    out = await HANDLERS["query"]({"query": {"text": "kitchen sink", "journals": {}, "journals@shaula": {}}}, PEER)
    assert out["journals@shaula"]["exact"] == [] and out["journals@shaula"]["error"] and peers.calls == []
    assert every(out)


def clash(gateway, older, newer):
    gateway.handlers[("journal", "tag_list")] = lambda args: {"tags": [], "clashes": [
        {"name": "ui", "older_id": "a", "newer_id": "b", "older_node": older, "newer_node": newer}]}


async def test_a_peer_an_open_tag_clash_stands_between_is_refused(served, gateway):
    clash(gateway, "altair", "local")
    with pytest.raises(PermissionError, match="tag clash"):
        await ask("kitchen sink", PEER)
    with pytest.raises(PermissionError, match="tag clash"):
        await HANDLERS["get"]({"source": "journals", "id": "j1"}, PEER)
    assert every(await ask("kitchen sink", "peer:shaula"))
    assert every(await ask("kitchen sink", LOCAL))


async def test_a_clash_between_two_other_nodes_refuses_nothing(served, gateway):
    clash(gateway, "altair", "shaula")
    assert every(await ask("kitchen sink", PEER))


async def test_a_node_an_open_tag_clash_stands_between_is_not_asked(served, gateway, peers):
    clash(gateway, "local", "altair")
    peers.add("altair", lambda args: {"journals": {"exact": [], "related": []}})
    peers.add("shaula", lambda args: {"journals": {"exact": [{"id": "r1", "title": "T", "ts": ""}]}})
    out = await HANDLERS["query"]({"query": {"text": "kitchen sink", "journals@altair": {},
                                             "journals@shaula": {}}}, LOCAL)
    assert "tag clash" in out["journals@altair"]["error"]
    assert out["journals@shaula"]["exact"] == [{"id": "r1", "title": "T", "ts": ""}]
    assert [c[0] for c in peers.calls] == ["shaula"]
    with pytest.raises(PermissionError, match="tag clash"):
        await HANDLERS["get"]({"source": "journals", "id": "r1", "node": "altair"}, LOCAL)


async def test_a_journal_that_cannot_list_its_clashes_refuses_nothing(served, gateway):
    gateway.handlers[("journal", "tag_list")] = lambda args: RuntimeError("down")
    assert every(await ask("kitchen sink", PEER))


async def test_a_relayed_query_over_a_hidden_tag_finds_nothing_by_the_tag_field(served):
    assert await found(PEER, tag="Avatar") == set()
    assert await found(LOCAL, tag="Avatar") == {"p2"}


async def test_get_hides_a_document_tagged_with_a_marked_project_from_a_peer(served):
    with pytest.raises(ValueError, match="^no document journals:p2$"):
        await HANDLERS["get"]({"source": "journals", "id": "p2"}, PEER)
    assert (await HANDLERS["get"]({"source": "journals", "id": "p3"}, PEER))["document"]["title"] == "Near tag"
    assert (await HANDLERS["get"]({"source": "journals", "id": "p2"}, LOCAL))["document"]["title"] == "Tagged"
    with pytest.raises(ValueError, match="^no document journals:j9$"):
        await HANDLERS["get"]({"source": "journals", "id": "j9"}, PEER)


async def test_unmarking_shows_a_tagged_document_again_without_a_resync(served):
    privacy.write_marks([])
    assert await found(PEER) == ALL


@pytest.mark.parametrize("project, scope", [
    ("avatar", ""), ("avatar", "dev"), ("avatar2", "dev"),
    ("awm", "svc-a"), ("awm", "svc-a/x"), ("awm", "svc-ab"),
    ("awm", ""), ("", ""),
])
def test_the_store_veil_and_the_live_hit_check_agree(tmp_path, root, project, scope):
    from awm.search import archive, lance_query, lance_store
    from awm.search.registry import parse_fields
    marks = [{"project": "avatar", "scope": None, "path": str(root / "avatar")},
             {"project": "awm", "scope": "svc-a", "path": str(root / "awm" / "svc-a")}]
    fields, hit = parse_fields({"fields": {"text": "content", "project": "categorical",
                                           "scope": {"class": "categorical", "nested": "/"}}})
    tables = lance_store.Store(tmp_path / "lance").tables("t", fields, hit)
    tables.upsert([{"id": "x", "text": "body", "project": project, "scope": scope}], None)
    kept = tables.count(lance_query.veil_sql(fields, marks))
    assert archive._covered(marks, project, scope) == (not kept)


async def test_a_get_is_logged_with_the_callers_scope_and_query_and_a_peers_relayed_get_is_not(served):
    await HANDLERS["get"]({"source": "journals", "id": "j1", "query": "kitchen sink",
                           "_caller_scope": "awm/dev"}, LOCAL)
    await HANDLERS["get"]({"source": "journals", "id": "j1", "_caller_scope": "awm/dev"}, PEER)
    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT project, scope, query, source, doc_id, node FROM usage")]
    finally:
        conn.close()
    assert rows == [{"project": "awm", "scope": "dev", "query": "kitchen sink", "source": "journals", "doc_id": "j1",
                     "node": "local"}]
