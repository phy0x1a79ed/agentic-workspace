"""Scopes search index: scopes and projects through the retrieval engine."""

from __future__ import annotations

from pathlib import Path


def test_scope_is_found_by_its_context_and_goal(seeded_scopes, monkeypatch):
    from awm.scopes import scopes, search_index
    worktree = Path(seeded_scopes[0][4])
    (worktree / ".awm").mkdir()
    (worktree / ".awm" / "context.md").write_text("# proj-a/scope-1\n\nOwns the sonar calibration rig.")
    monkeypatch.setattr(search_index, "_scope_objectives",
                        lambda project, scope: ["Ship the bathymetry export"]
                        if (project, scope) == ("proj-a", "scope-1") else [])
    search_index.index_scope("proj-a", "scope-1")
    search_index.flush()
    found = scopes.search_scopes(query="sonar rig calibration", status="all")
    assert found.scopes[0].scope == "scope-1"
    found = scopes.search_scopes(query="bathymetry", status="all")
    assert found.scopes[0].scope == "scope-1"
    assert scopes.search_scopes(query="sonar", status="completed").scopes == []


def test_a_silent_journal_service_leaves_the_context_to_index(monkeypatch):
    from awm.scopes import search_index

    def refuse(*a, **k):
        raise RuntimeError("no gateway")

    monkeypatch.setattr("awm.gatewayclient.call_sync", refuse)
    assert search_index._scope_objectives("proj-a", "scope-1") == []


def test_scope_objectives_come_from_the_journal_goal_read(monkeypatch):
    from awm.scopes import search_index
    sent = {}

    def fake(service, fn, args=None, **kw):
        sent.update(service=service, fn=fn, args=args)
        return {"goals": [{"objective": "a"}, {"objective": "b"}]}

    monkeypatch.setattr("awm.gatewayclient.call_sync", fake)
    assert search_index._scope_objectives("proj-a", "scope-1") == ["a", "b"]
    assert (sent["service"], sent["fn"]) == ("journal", "goal_read")
    assert sent["args"]["levels"] == ["scope"]


def test_reindex_backfills_scopes_and_projects(seeded_scopes):
    from awm.scopes import search_index
    counts = search_index.run_reindex(dry_run=True)
    assert set(counts) == {"scope", "project"}
    assert counts["scope"]["missing"] == 3
    search_index.run_reindex()
    assert search_index.run_reindex(dry_run=True)["scope"]["current"] == 3
