"""One-time seed: carry agent_instances from the legacy shared state.db
into the agents service's own DB.

agent_transcript has no legacy source — start empty.

Idempotent (INSERT OR IGNORE). Run against a COPY of any live state.db.

Usage:
    python -m awm.agents.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from awm.agents.dao import AgentsDAO, init


def seed_from_legacy(legacy_db: str | Path | None = None) -> int:
    """Seed agent_instances from legacy state.db.

    Returns the number of rows seeded. Rows whose agent_id cannot be
    resolved to (project, scope) are skipped with a LOUD warning.
    """
    if legacy_db is None:
        from awm.config import DB_PATH
        legacy_db = DB_PATH
    legacy_db = Path(legacy_db)
    init()
    if not legacy_db.exists():
        print(f"[seed] Legacy DB not found at {legacy_db}; nothing to seed.")
        return 0

    src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        # Build agent_id → (project, scope) lookup from the legacy identity join.
        id_map: dict[str, tuple[str, str]] = {}
        try:
            rows = src.execute(
                "SELECT a.id AS agent_id, p.name AS project, a.scope AS scope "
                "FROM agents a "
                "JOIN projects p ON p.id = a.project_id"
            ).fetchall()
            for r in rows:
                id_map[r["agent_id"]] = (r["project"], r["scope"])
        except sqlite3.OperationalError as exc:
            print(f"[seed] WARNING: Could not build agent_id map: {exc}")

        # Check agent_instances table exists.
        tbl = src.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='agent_instances'"
        ).fetchone()
        if tbl is None:
            print("[seed] No agent_instances table in legacy DB; nothing to seed.")
            return 0

        instances = src.execute(
            "SELECT agent_id, cli_session_id, log_path, "
            "       started_at, ended_at, data "
            "FROM agent_instances"
        ).fetchall()
    finally:
        src.close()

    dao = AgentsDAO()
    n = 0
    skipped = 0
    with dao.transaction():
        for r in instances:
            agent_id = r["agent_id"]
            identity = id_map.get(agent_id)
            if identity is None:
                print(
                    f"[seed] WARN: Cannot resolve agent_id={agent_id!r} "
                    f"to (project, scope) — skipping row."
                )
                skipped += 1
                continue
            project, scope = identity

            # Parse data column; default to intent=live.
            try:
                data = json.loads(r["data"] or "{}")
            except (TypeError, ValueError):
                data = {}

            # INSERT OR IGNORE so re-runs are safe (no unique key beyond id,
            # but we use started_at + project + scope as the dedup heuristic).
            # Since id is AUTOINCREMENT we can't directly reuse legacy ids.
            # Instead check for an existing row with same (project, scope, started_at).
            existing = dao.query_one(
                "SELECT id FROM agent_instances "
                "WHERE project=? AND scope=? AND started_at=?",
                (project, scope, r["started_at"]),
            )
            if existing:
                continue  # already seeded

            dao.execute(
                "INSERT INTO agent_instances "
                "(project, scope, cli_session_id, log_path, started_at, ended_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project, scope,
                    r["cli_session_id"],
                    r["log_path"],
                    r["started_at"],
                    r["ended_at"],
                    json.dumps(data, sort_keys=True),
                ),
            )
            n += 1

    print(f"[seed] Seeded {n} agent_instances row(s); skipped {skipped} unresolvable.")
    return n


def main() -> None:
    legacy = sys.argv[1] if len(sys.argv) > 1 else None
    seed_from_legacy(legacy)


if __name__ == "__main__":
    main()
