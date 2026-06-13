"""``BaseDAO`` — own-or-passed-connection data-access base.

Feature services subclass this in their own dist (e.g.
``class ArtifactDAO(BaseDAO)``) to hang their SQL off it. The base is
deliberately generic: NO feature-specific SQL lives here.

The own-or-passed pattern is hoisted from the ``conn: sqlite3.Connection |
None = None`` / ``own = conn is None`` idiom used across the scopes identity
layer. When a method is handed a connection it borrows it (no commit/close —
the owner manages the transaction); when it isn't, it opens one against the
DAO's own service DB, commits on success, and closes on the way out.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from awm.persistence.databases import get_connection


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class BaseDAO:
    """Base data-access object bound to a single service's DB.

    Subclasses provide feature-specific query methods that call the generic
    helpers below (:meth:`query_one`, :meth:`query_all`, :meth:`execute`,
    :meth:`executemany`) or open a managed unit of work via
    :meth:`transaction`.

    Each helper accepts an optional ``conn``: pass one to run inside a caller's
    open transaction (the caller commits), omit it to run as a standalone
    autocommitting statement against ``get_connection(self.service)``.
    """

    def __init__(self, service: str, conn: sqlite3.Connection | None = None) -> None:
        self.service = service
        # A DAO-level connection (e.g. one shared inside ``with dao.transaction()``)
        # supersedes per-call opening when set.
        self._conn = conn

    # -- connection plumbing -------------------------------------------------

    @contextmanager
    def _cursor(
        self, conn: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        """Yield a connection, owning it (commit+close) only when we opened it.

        Resolution order: an explicit ``conn`` arg → a DAO-level ``self._conn``
        (set inside :meth:`transaction`) → a freshly opened service connection.
        """
        borrowed = conn if conn is not None else self._conn
        own = borrowed is None
        c = borrowed if borrowed is not None else get_connection(self.service)
        try:
            yield c
            if own:
                c.commit()
        finally:
            if own:
                c.close()

    @contextmanager
    def transaction(
        self, conn: sqlite3.Connection | None = None
    ) -> Iterator[sqlite3.Connection]:
        """A unit of work: every helper called inside reuses one connection.

        Commits on clean exit, rolls back on exception. If ``conn`` is passed
        the caller owns the lifecycle (no commit/close here); otherwise a fresh
        service connection is opened, bound on the DAO for the duration, and
        committed/closed on exit.
        """
        if conn is not None:
            prev = self._conn
            self._conn = conn
            try:
                yield conn
            finally:
                self._conn = prev
            return

        c = get_connection(self.service)
        prev = self._conn
        self._conn = c
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._conn = prev
            c.close()

    # -- generic query helpers ----------------------------------------------

    def query_one(
        self,
        sql: str,
        params: Iterable[Any] = (),
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Run ``sql`` and return the first row as a plain dict (or None)."""
        with self._cursor(conn) as c:
            return _row_to_dict(c.execute(sql, tuple(params)).fetchone())

    def query_all(
        self,
        sql: str,
        params: Iterable[Any] = (),
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Run ``sql`` and return every row as a list of plain dicts."""
        with self._cursor(conn) as c:
            return [dict(r) for r in c.execute(sql, tuple(params)).fetchall()]

    def execute(
        self,
        sql: str,
        params: Iterable[Any] = (),
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Run a write statement.

        Returns ``lastrowid`` for an INSERT (when non-zero), else the affected
        ``rowcount``.
        """
        with self._cursor(conn) as c:
            cur = c.execute(sql, tuple(params))
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def executemany(
        self,
        sql: str,
        seq_of_params: Iterable[Iterable[Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Run ``sql`` once per parameter tuple. Returns total ``rowcount``."""
        with self._cursor(conn) as c:
            cur = c.executemany(sql, [tuple(p) for p in seq_of_params])
            return cur.rowcount
