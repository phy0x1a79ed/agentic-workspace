"""The plug-in registry: sources come from the `archive` tables of the running services."""

from __future__ import annotations

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


def test_the_internal_name_is_found_behind_the_published_one():
    e = entry("journal", [index_table("journals", "journal")])
    e["api"]["functions"] = [{"name": "dump", "tool": "journal_export"}]
    assert registry.parse([e])[0]["journals"].export == "dump"


def test_a_service_that_is_not_ready_is_not_running():
    sources, running = registry.parse([entry("kb", [index_table("kb", "kb")], status="starting")])
    assert sources == {} and running == set()


def test_a_non_service_registration_is_ignored():
    page = {"name": "landing", "kind": "page", "backend_status": "ready", "archive": []}
    assert registry.parse([page]) == ({}, set())


def test_a_live_source_names_its_query_verb():
    e = entry("transcripts", [{"name": "transcripts", "mode": "live", "query": "transcripts_query",
                               "optin": True}], verbs=("query",))
    src = registry.parse([e])[0]["transcripts"]
    assert (src.mode, src.query, src.optin) == ("live", "query", True)


@pytest.mark.parametrize("table, fragment", [
    ({"name": "Bad Name", "mode": "index", "export": "s_export"}, "must match"),
    ({"name": "x", "mode": "stream", "export": "s_export"}, "mode"),
    ({"name": "x", "mode": "index"}, "names none"),
    ({"name": "x", "mode": "index", "export": "s_missing"}, "does not publish"),
    ({"name": "x", "mode": "live", "query": "s_missing"}, "does not publish"),
])
def test_an_unusable_table_is_listed_with_its_error(table, fragment):
    sources, _ = registry.parse([entry("s", [table])])
    (src,) = sources.values()
    assert fragment in src.error


def test_a_second_service_cannot_take_a_name_held():
    sources, _ = registry.parse([entry("a", [index_table("same", "a")]), entry("b", [index_table("same", "b")])])
    assert sources["same"].service == "a"


def test_a_registry_without_archive_keys_falls_back_to_the_manifest(tmp_path, monkeypatch):
    folder = tmp_path / "writing"
    folder.mkdir()
    (folder / "service.toml").write_text('[[archive]]\nname = "writing"\nmode = "index"\nexport = "writing_export"\n')
    monkeypatch.setenv("AWM_SERVICES_DIR", str(tmp_path))
    sources, _ = registry.parse([entry("writing", archive=False)])
    assert sources["writing"].export == "export"


def test_a_spec_survives_storage():
    src = registry.parse([entry("journal", [index_table("journals", "journal", topic="posts")])])[0]["journals"]
    assert registry.Source.from_spec(src.spec()) == src


SERVICES = Path(__file__).resolve().parents[2]


def _tables():
    import tomllib
    for toml in sorted(SERVICES.glob("*/service.toml")):
        for table in tomllib.loads(toml.read_text()).get("archive") or []:
            yield toml.parent.name, table


def test_every_table_in_the_tree_names_a_verb_its_service_publishes():
    tables = list(_tables())
    assert {t["name"] for _, t in tables} >= {"journals", "precedence", "writing", "notes", "transcripts"}
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
