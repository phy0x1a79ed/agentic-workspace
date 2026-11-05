"""The manifest and handlers: what the gateway sees of the search service."""

from __future__ import annotations

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import API_MANIFEST, HANDLERS

from .conftest import doc, synced

pytestmark = [pytest.mark.search]

FNS = {f["name"]: f for f in API_MANIFEST["functions"]}


def mcp_verbs() -> set[str]:
    return {n for n, f in FNS.items() if "surfaces" not in f}


def test_the_mcp_surface_is_query_get_and_sources():
    assert mcp_verbs() == {"query", "get", "sources"}


def test_the_operator_verbs_stay_on_the_cli_and_http():
    for name in ("status", "reindex", "judge", "judgements"):
        assert FNS[name]["surfaces"] == ["cli", "http"], name


def test_every_verb_is_published_under_the_search_tool_and_wired():
    assert set(HANDLERS) == set(FNS) == {"query", "get", "sources", "status", "reindex", "judge", "judgements"}
    for name, fn in FNS.items():
        assert fn["tool"] == f"search_{name}"


def test_the_query_verb_takes_query_and_max_and_carries_the_agreed_description():
    params = {p["name"]: p for p in FNS["query"]["params"]}
    assert list(params) == ["query", "max"]
    assert params["query"]["required"] and params["max"]["type"] == "integer" and not params["max"].get("required")
    text = FNS["query"]["description"]
    assert text == hub_adapter.QUERY_DESCRIPTION
    assert len(text.splitlines()) == 12
    assert text.startswith("search query \u2014 rank items from named sources.\n")
    assert text.endswith("Open one with `get`.\n")
    assert "Short forms: query s = " in text and "`related`" in text


def test_get_takes_source_id_node_and_query_and_no_context():
    assert [p["name"] for p in FNS["get"]["params"]] == ["source", "id", "node", "query"]
    assert [p["name"] for p in FNS["get"]["params"] if p.get("required")] == ["source", "id"]


class Conn:
    def close(self):
        pass


@pytest.fixture()
def logged(monkeypatch):
    rows = []
    monkeypatch.setattr(hub_adapter.store, "connect", lambda: Conn())
    monkeypatch.setattr(hub_adapter.store, "log_use", lambda conn, row: rows.append(row))
    return rows


def test_a_get_is_logged_with_the_callers_project_and_scope(logged):
    hub_adapter._log_get({"source": "journals", "id": "j1", "query": " sink ",
                          "_caller_scope": "awm/redux/search"}, "local")
    assert logged == [{"project": "awm", "scope": "redux/search", "query": "sink", "source": "journals",
                       "doc_id": "j1", "node": "local"}]


def test_a_get_without_a_caller_scope_is_logged_with_none(logged):
    hub_adapter._log_get({"source": "journals", "id": "j1"}, "local")
    assert (logged[0]["project"], logged[0]["scope"], logged[0]["query"]) == ("", "", "")


async def test_a_relayed_get_is_not_logged_by_the_serving_node(logged, monkeypatch):
    class Stub:
        async def refuse_clashed(self, node):
            pass

        async def get(self, source, doc_id, hidden):
            return {"document": {"title": "t"}}

    monkeypatch.setattr(hub_adapter, "ARCHIVE", Stub())
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    args = {"source": "journals", "id": "j1", "_caller_scope": "awm/dev"}
    await HANDLERS["get"](args, "peer:shaula")
    assert logged == []
    await HANDLERS["get"](args, None)
    assert [r["doc_id"] for r in logged] == ["j1"]


async def test_query_hands_the_parsed_request_to_the_archive(monkeypatch):
    seen = []

    class Stub:
        async def query(self, req, caller):
            seen.append((req, caller))
            return {"journals": {"exact": []}}

    monkeypatch.setattr(hub_adapter, "ARCHIVE", Stub())
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    out = await HANDLERS["query"]({"query": {"text": "sink", "journals": {"scope": "redux"}}, "max": 3})
    assert out == {"journals": {"exact": []}}
    req, caller = seen[0]
    assert req.max == 3 and req.sources[0].filter == {"scope": "redux"} and caller is None


async def test_a_malformed_query_comes_back_as_a_value_error(monkeypatch):
    class Stub:
        async def query(self, req, caller):
            raise AssertionError("a malformed query must not reach the archive")

    monkeypatch.setattr(hub_adapter, "ARCHIVE", Stub())
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    with pytest.raises(ValueError, match="journals"):
        await HANDLERS["query"]({"query": {"journals": {"nonesuch": 1}}})


async def test_sources_lists_fields_and_health_and_not_the_retired(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    gateway.plug("precedence", {"p1": doc("Sink", "kitchen sink precedent")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    out = await HANDLERS["sources"]({})
    by_name = {s["name"]: s for s in out["sources"]}
    assert set(by_name) == {"journals"}
    row = by_name["journals"]
    assert row["kind"] == "journals" and row["node"] and row["count"] == 1
    assert row["fields"]["tag"] == "multi" and row["fields"]["project"] == "single"
    assert {"last_sync", "frozen", "last_error"} <= set(row)
    assert "registry" in out and "semantic" in out and "sweeping" in out


def test_the_judgement_verbs_are_closed_to_peers():
    for name in ("judge", "judgements"):
        assert "category" not in FNS[name], name
    assert FNS["judgements"]["surfaces"] == ["cli", "http"]


async def test_a_verdict_is_recorded_replaced_and_cleared(search_db):
    mark = {"query": "kitchen sink", "source": "journals", "id": "j1", "verdict": "relevant",
            "section": "exact", "set": "gold"}
    await HANDLERS["judge"](mark, "scope:awm/svc-kb")
    await HANDLERS["judge"]({**mark, "verdict": "irrelevant", "section": ""}, "tony")
    await HANDLERS["judge"]({**mark, "id": "j2"})
    rows = (await HANDLERS["judgements"]({"set": "gold"}))["judgements"]
    assert [(r["doc_id"], r["verdict"], r["section"], r["judge"]) for r in rows] == \
        [("j1", "irrelevant", "", "tony"), ("j2", "relevant", "exact", "")]
    assert (await HANDLERS["judge"]({**mark, "verdict": "clear"}))["cleared"] == 1
    assert [r["doc_id"] for r in (await HANDLERS["judgements"]({}))["judgements"]] == ["j2"]
    assert (await HANDLERS["judgements"]({"set": "t5"}))["judgements"] == []


async def test_a_bad_verdict_or_section_is_refused(search_db):
    mark = {"query": "q", "source": "journals", "id": "j1", "verdict": "relevant"}
    with pytest.raises(ValueError, match="verdict"):
        await HANDLERS["judge"]({**mark, "verdict": "maybe"})
    with pytest.raises(ValueError, match="section"):
        await HANDLERS["judge"]({**mark, "section": "abstract"})
    with pytest.raises(ValueError, match="needs"):
        await HANDLERS["judge"]({**mark, "id": ""})


async def test_the_handlers_answer_an_admitted_peer_every_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    gateway.plug("wiki", {"p1": doc("Sink", "kitchen sink precedent")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    result = await HANDLERS["query"]({"query": "kitchen sink"}, "peer:shaula")
    assert [h["id"] for h in result["journals"]["exact"]] == ["j1"]
    assert {s["name"] for s in (await HANDLERS["sources"]({}, "peer:shaula"))["sources"]} == {"journals", "wiki"}
    status = await HANDLERS["status"]({}, "peer:shaula")
    assert status["sources"] == {"journals": 1, "wiki": 1} and "registry" in status
    got = await HANDLERS["get"]({"source": "wiki", "id": "p1"}, "peer:shaula")
    assert got["document"]["origin"] == {"node": "local"}


async def test_reindex_queues_a_pass_and_names_a_bad_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    out = await HANDLERS["reindex"]({"source": "journals", "force": "true"})
    assert out["resync"] == ["journals"] and out["engine"]["documents"] == 1
    assert archive._due["journals"] <= archive.now()
    with pytest.raises(ValueError, match="not a running index source"):
        await HANDLERS["reindex"]({"source": "nothing"})
