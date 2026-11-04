"""Journal search index: the old engine's post embeddings, kept current until it retires."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


def test_reindex_backfills_and_prunes(journal_workspace, journal_conn):
    from awm.journal import search_index
    from tests.conftest import insert_post
    insert_post(journal_conn, "seeded", "proj-a", "scope-1", "system", "journal",
                "imported from state.db", "{}", 1)
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
    blocker = search_index.INDEX.worker.submit(time.sleep, 0.2)
    p = channel.post("awm", "dev", author="user:a", tags="message", body="queued before the switch")
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
