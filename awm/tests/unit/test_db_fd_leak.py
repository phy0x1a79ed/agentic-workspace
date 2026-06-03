"""Regression test for the cr-sqlite FD leak in get_connection.

awm-exposed.service wedged after ~13h with 32,760 leaked handles to
state.db and another 32,758 to state.db-wal because every cr-sqlite
connection that closes without `SELECT crsql_finalize()` leaves those
FDs open. get_connection now subclasses sqlite3.Connection to finalize
on close — this test asserts the leak stays plugged.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import os
from pathlib import Path

import pytest

from awm.db import get_connection, init_db


def _count_db_fds(db_path: Path) -> int:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("/proc/self/fd not available on this platform")
    target = str(db_path)
    n = 0
    for entry in fd_dir.iterdir():
        try:
            link = os.readlink(entry)
        except OSError:
            continue
        if link.startswith(target):
            n += 1
    return n


def test_get_connection_does_not_leak_fds(tmp_path):
    db = tmp_path / "state.db"
    init_db(db_path=db)
    baseline = _count_db_fds(db)
    for _ in range(200):
        c = get_connection(db_path=db)
        c.execute("SELECT 1").fetchone()
        c.close()
    leaked = _count_db_fds(db) - baseline
    assert leaked <= 2, (
        f"leaked {leaked} state.db FDs across 200 open/close cycles "
        f"(baseline={baseline})"
    )


def test_get_connection_close_is_idempotent_with_explicit_finalize(tmp_path):
    """The init code in db.init_db and replication.sync already call
    finalize() before close. With the close wrapper they'd call it again
    via super().close()'s path — confirm that doesn't error."""
    from awm.services.replication import schema as repl_schema

    db = tmp_path / "state.db"
    init_db(db_path=db)
    conn = get_connection(db_path=db)
    repl_schema.finalize(conn)
    # No exception — wrapper will call finalize again and then super().close().
    conn.close()
