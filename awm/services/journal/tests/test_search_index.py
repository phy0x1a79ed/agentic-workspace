"""Journal search index: posts through the retrieval engine."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


def _long_journal(detail: str) -> str:
    filler = "\n\n".join(f"Step {i}: rebuilt the widget pipeline and reran checks." for i in range(40))
    return f"{filler}\n\n## Issues\n{detail}"


def test_detail_deep_in_a_journal_is_found(journal_workspace):
    from awm.journal import channel, search_index
    target = channel.post("awm", "dev", author="agent:awm/dev", kind="journal",
                          body=_long_journal("The zeppelin mooring mast leaked hydraulic fluid."),
                          meta={"title": "Hangar work"})
    for i in range(5):
        channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body=f"routine day {i}")
    search_index.flush()
    posts, degraded, _ = channel.search(query="mooring mast leak", project="awm", kind="journal")
    assert posts[0].id == target.id
    assert "zeppelin" in posts[0].match["snippet"]
    assert degraded is None


def test_filters_apply_before_ranking(journal_workspace):
    from awm.journal import channel, search_index
    for i in range(20):
        channel.post("awm", "dev", author="user:a", kind="message", body=f"telescope mirror {i}")
    j = channel.post("other", "x", author="agent:other/x", kind="journal", body="the telescope")
    search_index.flush()
    posts, _, _ = channel.search(query="telescope mirror", kind="journal", limit=3)
    assert [p.id for p in posts] == [j.id]


def test_system_posts_fall_back_to_substring(journal_workspace):
    from awm.journal import channel
    channel.post("awm", "dev", author="system", kind="system", body="worktree healed")
    posts, _, _ = channel.search(query="healed", kind="system")
    assert [p.body for p in posts] == ["worktree healed"]


def test_reindex_backfills_and_prunes(journal_workspace, journal_conn):
    from awm.journal import search_index
    journal_conn.execute(
        "INSERT INTO scope_posts (id, owner_project, owner_scope, author, kind, body, meta, ts)"
        " VALUES ('seeded', 'proj-a', 'scope-1', 'system', 'journal', 'imported from state.db', '{}', 1)")
    journal_conn.commit()
    counts = search_index.run_reindex(dry_run=True)
    assert counts["post"]["missing"] == 1
    search_index.run_reindex()
    assert search_index.run_reindex(dry_run=True)["post"]["current"] == 1
    journal_conn.execute("DELETE FROM scope_posts WHERE id='seeded'")
    journal_conn.commit()
    assert search_index.run_reindex()["post"]["pruned"] == 1


def test_queued_write_keeps_the_database_it_was_queued_for(journal_workspace, monkeypatch, tmp_path):
    import sqlite3
    import time
    import awm.persistence.databases as pdbs
    from awm.journal import channel, search_index
    blocker = search_index._worker.submit(time.sleep, 0.2)
    p = channel.post("awm", "dev", author="user:a", kind="message", body="queued before the switch")
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    monkeypatch.setattr(pdbs, "SERVICES_DIR", moved)
    blocker.result()
    search_index.flush()
    assert not (moved / "journal").exists()
    conn = sqlite3.connect(journal_workspace["services_dir"] / "journal" / "journal.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id=?", (p.id,)).fetchone()[0] == 1
    finally:
        conn.close()
