"""Scopes search index: posts, scopes and projects through the retrieval engine."""

from __future__ import annotations

from pathlib import Path


def _long_journal(detail: str) -> str:
    filler = "\n\n".join(f"Step {i}: rebuilt the widget pipeline and reran checks." for i in range(40))
    return f"{filler}\n\n## Issues\n{detail}"


def test_detail_deep_in_a_journal_is_found(scopes_workspace):
    from awm.scopes import channel, search_index
    target = channel.post("awm", "dev", author="agent:awm/dev", kind="journal",
                          body=_long_journal("The zeppelin mooring mast leaked hydraulic fluid."),
                          meta={"title": "Hangar work"})
    for i in range(5):
        channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body=f"routine day {i}")
    search_index.flush()
    posts, degraded = channel.search(query="mooring mast leak", project="awm", kind="journal")
    assert posts[0].id == target.id
    assert "zeppelin" in posts[0].match["snippet"]
    assert degraded is None


def test_filters_apply_before_ranking(scopes_workspace):
    from awm.scopes import channel, search_index
    for i in range(20):
        channel.post("awm", "dev", author="user:a", kind="message", body=f"telescope mirror {i}")
    j = channel.post("other", "x", author="agent:other/x", kind="journal", body="the telescope")
    search_index.flush()
    posts, _ = channel.search(query="telescope mirror", kind="journal", limit=3)
    assert [p.id for p in posts] == [j.id]


def test_system_posts_fall_back_to_substring(scopes_workspace):
    from awm.scopes import channel
    channel.post("awm", "dev", author="system", kind="system", body="worktree healed")
    posts, _ = channel.search(query="healed", kind="system")
    assert [p.body for p in posts] == ["worktree healed"]


def test_scope_is_found_by_its_context_and_goal(seeded_scopes):
    from awm.scopes import goals, scopes, search_index
    worktree = Path(seeded_scopes[0][4])
    (worktree / ".awm").mkdir()
    (worktree / ".awm" / "context.md").write_text("# proj-a/scope-1\n\nOwns the sonar calibration rig.")
    goals.set_goal(objective="Ship the bathymetry export", author="user:a",
                   level="scope", project="proj-a", scope="scope-1")
    search_index.flush()
    found = scopes.search_scopes(query="sonar rig calibration", status="all")
    assert found.scopes[0].scope == "scope-1"
    found = scopes.search_scopes(query="bathymetry", status="all")
    assert found.scopes[0].scope == "scope-1"
    assert scopes.search_scopes(query="sonar", status="completed").scopes == []


def test_reindex_backfills_and_prunes(seeded_scopes, scopes_dao_conn):
    from awm.scopes import search_index
    scopes_dao_conn.execute(
        "INSERT INTO scope_posts (id, owner_project, owner_scope, author, kind, body, meta, ts)"
        " VALUES ('seeded', 'proj-a', 'scope-1', 'system', 'journal', 'imported from state.db', '{}', 1)")
    scopes_dao_conn.commit()
    counts = search_index.run_reindex(dry_run=True)
    assert counts["post"]["missing"] == 1
    assert counts["scope"]["missing"] == 3
    search_index.run_reindex()
    assert search_index.run_reindex(dry_run=True)["post"]["current"] == 1
    scopes_dao_conn.execute("DELETE FROM scope_posts WHERE id='seeded'")
    scopes_dao_conn.commit()
    assert search_index.run_reindex()["post"]["pruned"] == 1


def test_queued_write_keeps_the_database_it_was_queued_for(scopes_workspace, monkeypatch, tmp_path):
    import sqlite3
    import time
    import awm.persistence.databases as pdbs
    from awm.scopes import channel, search_index
    blocker = search_index._worker.submit(time.sleep, 0.2)
    p = channel.post("awm", "dev", author="user:a", kind="message", body="queued before the switch")
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    monkeypatch.setattr(pdbs, "SERVICES_DIR", moved)
    blocker.result()
    search_index.flush()
    assert not (moved / "scopes").exists()
    conn = sqlite3.connect(scopes_workspace["services_dir"] / "scopes" / "scopes.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id=?", (p.id,)).fetchone()[0] == 1
    finally:
        conn.close()
