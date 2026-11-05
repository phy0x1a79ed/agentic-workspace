"""The plug-in registry: sources come from the `archive` tables of the running services."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from awm.search import registry

from .conftest import entry, index_table

pytestmark = [pytest.mark.search]


def test_a_ready_service_with_a_table_becomes_a_source():
    sources, running = registry.parse([entry("journal", [index_table("journals", "journal", topic="posts")])])
    assert running == {"journal"}
    src = sources["journals"]
    assert (src.service, src.mode, src.export, src.topic, src.error) == ("journal", "index", "export", "posts", "")


def test_a_source_is_called_by_its_published_tool_and_verb():
    e = entry("journal", [index_table("journals", "journal")], tools=("journal",))
    src = registry.parse([e])[0]["journals"]
    assert (src.tool, src.export) == ("journal", "export")
    e = entry("papers", [{"name": "papers", "mode": "index", "export": "export"}])
    src = registry.parse([e])[0]["papers"]
    assert (src.tool, src.export) == ("papers", "export"), "a bare verb finds the service's one tool"
    e = entry("cc", [{"name": "cc", "mode": "index", "export": "harness_cc_export"}], tools=("harness", "harness_cc"))
    assert registry.parse([e])[0]["cc"].tool == "harness_cc", "the longest tool name wins"


def test_a_service_that_is_not_ready_is_not_running():
    sources, running = registry.parse([entry("kb", [index_table("kb", "kb")], status="starting")])
    assert sources == {} and running == set()


def test_a_live_source_names_its_query_verb():
    e = entry("transcripts", [{"name": "transcripts", "mode": "live", "query": "transcripts_query",
                               "optin": True}])
    src = registry.parse([e])[0]["transcripts"]
    assert (src.mode, src.query, src.optin) == ("live", "query", True)


@pytest.mark.parametrize("table, fragment", [
    ({"name": "Bad Name", "mode": "index", "export": "s_export"}, "must match"),
    ({"name": "x", "mode": "stream", "export": "s_export"}, "mode"),
    ({"name": "x", "mode": "index"}, "names none"),
    ({"name": "x", "mode": "index", "export": "other_export"}, "does not publish"),
    ({"name": "x", "mode": "live", "query": "other_query"}, "does not publish"),
    ({"name": "x", "mode": "live", "query": "s_query", "get": "other_get"}, "`get` names"),
])
def test_an_unusable_table_is_listed_with_its_error(table, fragment):
    sources, _ = registry.parse([entry("s", [table])])
    (src,) = sources.values()
    assert fragment in src.error


def test_a_second_service_cannot_take_a_name_held():
    sources, _ = registry.parse([entry("a", [index_table("same", "a")]), entry("b", [index_table("same", "b")])])
    assert sources["same"].service == "a"


def test_a_spec_survives_storage():
    src = registry.parse([entry("journal", [index_table("journals", "journal", topic="posts")])])[0]["journals"]
    assert registry.Source.from_spec(src.spec()) == src


SERVICES = Path(__file__).resolve().parents[2]


def _tables():
    for folder in sorted(p.parent for p in SERVICES.glob("*/run.sh")):
        try:
            plugin = tomllib.loads((folder / "awm-plugin.toml").read_text())
        except OSError:
            continue
        for svc in plugin.get("service") or []:
            if svc.get("name") == folder.name:
                for table in svc.get("archive") or []:
                    yield folder.name, table


def test_every_table_in_the_tree_names_a_verb_its_service_publishes():
    tables = list(_tables())
    assert {t["name"] for _, t in tables} >= {"journals", "transcripts"}
    for service, table in tables:
        verb = table["export"] if table["mode"] == "index" else table["query"]
        sources = "".join(p.read_text() for p in (SERVICES / service / "awm").rglob("*.py"))
        assert f'"tool": "{verb}"' in sources, (service, verb)
        assert registry._NAME.fullmatch(table["name"]), table


def test_an_unchanged_registry_writes_nothing_so_a_query_never_waits_on_a_sweep(search_db):
    import sqlite3

    from awm.search import store
    spec = {"journals": {"service": "journal", "mode": "index"}}
    conn = store.connect()
    assert store.record_registry(conn, spec, set(spec)) == ["journals"]
    conn.close()
    sweep = store.connect()
    sweep.execute("BEGIN IMMEDIATE")
    query = store.connect()
    query.execute("PRAGMA busy_timeout = 50")
    try:
        assert store.record_registry(query, spec, set(spec)) == []
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.record_registry(query, {}, set())
    finally:
        sweep.rollback()
        query.close()
        sweep.close()
