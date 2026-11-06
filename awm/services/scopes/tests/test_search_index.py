"""Scopes search index: scopes and projects through the retrieval engine."""

from __future__ import annotations


def test_scope_is_found_by_its_brief(seeded_scopes, monkeypatch):
    from awm.scopes import scopes, search_index
    monkeypatch.setattr(search_index, "_scope_brief",
                        lambda project, scope: "for the bathymetry export"
                        if (project, scope) == ("proj-a", "scope-1") else "")
    search_index.index_scope("proj-a", "scope-1")
    search_index.flush()
    found = scopes.search_scopes(query="bathymetry", status="all")
    assert found.scopes[0].scope == "scope-1"
    assert scopes.search_scopes(query="bathymetry", status="completed").scopes == []


def test_a_silent_journal_service_indexes_an_empty_brief(monkeypatch):
    from awm.scopes import search_index

    def refuse(*a, **k):
        raise RuntimeError("no gateway")

    monkeypatch.setattr("awm.gatewayclient.call_sync", refuse)
    assert search_index._scope_brief("proj-a", "scope-1") == ""


def test_reindex_backfills_scopes_and_projects(seeded_scopes):
    from awm.scopes import search_index
    counts = search_index.run_reindex(dry_run=True)
    assert set(counts) == {"scope", "project"}
    assert counts["scope"]["missing"] == 3
    search_index.run_reindex()
    assert search_index.run_reindex(dry_run=True)["scope"]["current"] == 3
