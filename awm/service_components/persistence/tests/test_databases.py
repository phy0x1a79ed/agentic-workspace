"""Connections: a close never checkpoints a live DB, and the keepers stay bounded."""

from __future__ import annotations

import pytest

from awm.persistence import databases


@pytest.fixture(autouse=True)
def fresh_keepers():
    databases.release_keepers()
    yield
    databases.release_keepers()


def _write(path):
    conn = databases.get_connection_at(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def _wal(path):
    return path.with_name(path.name + "-wal")


def test_closing_the_only_caller_connection_leaves_the_wal_unchecked(tmp_path):
    db = tmp_path / "svc.db"
    _write(db)
    assert _wal(db).exists()
    databases.release_keepers()
    assert not _wal(db).exists()


def test_connections_commit_without_a_full_sync(tmp_path):
    conn = databases.get_connection_at(tmp_path / "svc.db")
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    conn.close()


def test_the_keepers_close_the_oldest_beyond_their_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(databases, "_KEEPERS_MAX", 2)
    dbs = [tmp_path / f"{i}.db" for i in range(3)]
    for db in dbs:
        _write(db)
    assert [_wal(db).exists() for db in dbs] == [False, True, True]


def test_a_db_replaced_on_disk_gets_a_fresh_keeper(tmp_path):
    db = tmp_path / "svc.db"
    _write(db)
    for f in (db, _wal(db), db.with_name(db.name + "-shm")):
        f.unlink()
    _write(db)
    conn = databases.get_connection_at(db)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    conn.close()
    assert len(databases._keepers) == 1
