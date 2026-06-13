"""One-time seed: carry all scopes-owned rows from the legacy shared
``state.db`` into the scopes service's own DB.

The modular cutover drops the shared runtime DB but leaves the old file on
disk as a read-only legacy source. This module extracts every table the scopes
service owns — identity (projects/users/agents), session_logs, messages, rooms,
guest_list, room_transcripts — plus the scopes/session/room/project embeddings.

Re-keying rules (per SCHEMA_HANDOFF.md):
  - ``agent_id`` → ``(project, scope)`` via the identity join.
  - Polymorphic refs (messages.recipient_id / sender_id, guest_list.guest_ref,
    room_transcripts.author) → natural-key ref strings:
      agent uuid   → ``'agent:<project>/<scope>'``
      user uuid    → ``'user:<username>'``
      'system'     → ``'system'``
  - rooms.owner_agent_id → (owner_project, owner_scope) via identity join.

Unresolvable rows are SKIPPED with a loud log line; no orphans are written.

Idempotent (upsert / ON CONFLICT DO NOTHING), so re-running is safe.

Usage:
    python -m awm.scopes.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from awm.scopes.dao import ScopesDAO, init

log = logging.getLogger("awm.scopes.seed")

# Embeddings source_types owned by the scopes service.
_SCOPES_EMBED_TYPES = {"session", "scope", "room", "project"}


def _build_agent_map(src: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """Build agent_id → (project, scope) from the legacy DB."""
    rows = src.execute(
        "SELECT a.id AS agent_id, p.name AS project, a.scope AS scope "
        "FROM agents a JOIN projects p ON p.id = a.project_id"
    ).fetchall()
    return {r["agent_id"]: (r["project"], r["scope"]) for r in rows}


def _build_user_map(src: sqlite3.Connection) -> dict[str, str]:
    """Build user_id → username from the legacy DB."""
    rows = src.execute("SELECT id, username FROM users").fetchall()
    return {r["id"]: r["username"] for r in rows}


def _resolve_poly_ref(
    ref_id: str,
    agent_map: dict[str, tuple[str, str]],
    user_map: dict[str, str],
) -> str | None:
    """Resolve a polymorphic ref (agent uuid | user uuid | 'system') to a
    natural-key ref string.

    Returns the natural-key string, or None if unresolvable.
    """
    if ref_id == "system" or not ref_id:
        return "system"
    if ref_id in agent_map:
        proj, sc = agent_map[ref_id]
        return f"agent:{proj}/{sc}"
    if ref_id in user_map:
        return f"user:{user_map[ref_id]}"
    return None


def seed_from_legacy(legacy_db: str | Path | None = None) -> dict[str, int]:
    """Seed scopes service DB from the legacy ``state.db``.

    Returns a dict of table → rows seeded. A missing legacy file or missing
    table is a no-op (returns 0 for that table).
    """
    if legacy_db is None:
        from awm.config import DB_PATH
        legacy_db = DB_PATH
    legacy_db = Path(legacy_db)

    init()

    counts: dict[str, int] = {
        "projects": 0,
        "users": 0,
        "agents": 0,
        "session_logs": 0,
        "messages": 0,
        "rooms": 0,
        "guest_list": 0,
        "room_transcripts": 0,
        "embeddings": 0,
    }

    if not legacy_db.exists():
        log.warning("seed_from_legacy: %s does not exist; nothing to seed", legacy_db)
        return counts

    src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        return _do_seed(src, counts)
    finally:
        src.close()


def _do_seed(src: sqlite3.Connection, counts: dict[str, int]) -> dict[str, int]:
    dao = ScopesDAO()

    # --- 1. Identity tables (straight copy — uuid PKs preserved) ------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT id, name, url, repo_path, created_at FROM projects"
        ).fetchall():
            dao.execute(
                "INSERT OR IGNORE INTO projects (id, name, url, repo_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (r["id"], r["name"], r["url"], r["repo_path"] or "", r["created_at"]),
                conn=conn,
            )
            counts["projects"] += 1

        for r in src.execute("SELECT id, username FROM users").fetchall():
            dao.execute(
                "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
                (r["id"], r["username"]),
                conn=conn,
            )
            counts["users"] += 1

        for r in src.execute(
            "SELECT id, project_id, scope, parent_id, status, agent_cli, branch, "
            "worktree, display_name, is_vagrant, created_at, retired_at FROM agents"
        ).fetchall():
            dao.execute(
                "INSERT OR IGNORE INTO agents "
                "(id, project_id, scope, parent_id, status, agent_cli, branch, "
                " worktree, display_name, is_vagrant, created_at, retired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["project_id"], r["scope"], r["parent_id"],
                    r["status"], r["agent_cli"] or "claude", r["branch"] or "",
                    r["worktree"] or "", r["display_name"] or r["scope"],
                    r["is_vagrant"] or 0, r["created_at"], r["retired_at"],
                ),
                conn=conn,
            )
            counts["agents"] += 1

    # Build lookup maps AFTER identity tables are seeded (so the join works on
    # legacy data, not the new DB).
    agent_map = _build_agent_map(src)
    user_map = _build_user_map(src)

    # --- 2. Session logs -----------------------------------------------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT agent_id, created_at, file_path, git_commit, summary, metadata, "
            "content, skill_path, outcome, deviations, suggestions, skill_version, "
            "resolved_at, resolution, title FROM session_logs"
        ).fetchall():
            ps = agent_map.get(r["agent_id"])
            if ps is None:
                log.warning(
                    "seed session_logs: unresolvable agent_id=%s; skipping row",
                    r["agent_id"],
                )
                continue
            project, scope = ps
            dao.execute(
                "INSERT INTO session_logs "
                "(project, scope, created_at, file_path, git_commit, summary, "
                " metadata, content, skill_path, outcome, deviations, suggestions, "
                " skill_version, resolved_at, resolution, title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project, scope, r["created_at"], r["file_path"] or "",
                    r["git_commit"], r["summary"] or "", r["metadata"],
                    r["content"], r["skill_path"], r["outcome"],
                    r["deviations"], r["suggestions"], r["skill_version"],
                    r["resolved_at"], r["resolution"], r["title"],
                ),
                conn=conn,
            )
            counts["session_logs"] += 1

    # --- 3. Messages (re-key polymorphic refs) --------------------------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT recipient_id, sender_id, msg_type, subject, body, metadata, "
            "status, created_at, read_at FROM messages"
        ).fetchall():
            rec_ref = _resolve_poly_ref(r["recipient_id"], agent_map, user_map)
            send_ref = _resolve_poly_ref(r["sender_id"], agent_map, user_map)
            if rec_ref is None:
                log.warning(
                    "seed messages: unresolvable recipient_id=%s; skipping row",
                    r["recipient_id"],
                )
                continue
            if send_ref is None:
                log.warning(
                    "seed messages: unresolvable sender_id=%s; skipping row",
                    r["sender_id"],
                )
                continue
            dao.execute(
                "INSERT INTO messages "
                "(recipient_ref, sender_ref, msg_type, subject, body, metadata, "
                " status, created_at, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec_ref, send_ref, r["msg_type"] or "", r["subject"] or "",
                    r["body"] or "", r["metadata"], r["status"] or "unread",
                    r["created_at"], r["read_at"],
                ),
                conn=conn,
            )
            counts["messages"] += 1

    # --- 4. Rooms (re-key owner_agent_id → (owner_project, owner_scope)) -----

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT id, owner_agent_id, topic, status, created_at, closed_at FROM rooms"
        ).fetchall():
            ps = agent_map.get(r["owner_agent_id"])
            if ps is None:
                log.warning(
                    "seed rooms: unresolvable owner_agent_id=%s for room %s; skipping",
                    r["owner_agent_id"], r["id"],
                )
                continue
            owner_project, owner_scope = ps
            dao.execute(
                "INSERT OR IGNORE INTO rooms "
                "(id, owner_project, owner_scope, topic, status, created_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], owner_project, owner_scope,
                    r["topic"] or "", r["status"] or "active",
                    r["created_at"], r["closed_at"],
                ),
                conn=conn,
            )
            counts["rooms"] += 1

    # --- 5. guest_list (re-key guest_ref uuid → natural key) -----------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT room_id, guest_kind, guest_ref, display_name, subscriptions FROM guest_list"
        ).fetchall():
            # guest_ref may be an agent uuid or a user uuid
            natural_ref = _resolve_poly_ref(r["guest_ref"], agent_map, user_map)
            if natural_ref is None:
                log.warning(
                    "seed guest_list: unresolvable guest_ref=%s in room %s; skipping",
                    r["guest_ref"], r["room_id"],
                )
                continue
            # Strip 'agent:' prefix for agent guests (schema stores 'project/scope')
            if natural_ref.startswith("agent:"):
                stored_ref = natural_ref[len("agent:"):]
            elif natural_ref.startswith("user:"):
                stored_ref = natural_ref  # keep 'user:<name>' form
            else:
                stored_ref = natural_ref
            dao.execute(
                "INSERT OR IGNORE INTO guest_list "
                "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    r["room_id"], r["guest_kind"] or "agent",
                    stored_ref, r["display_name"] or "",
                    r["subscriptions"] or "{}",
                ),
                conn=conn,
            )
            counts["guest_list"] += 1

    # --- 6. room_transcripts (re-key polymorphic author) ----------------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT id, room_id, author, kind, body, meta, ts FROM room_transcripts"
        ).fetchall():
            natural_author = _resolve_poly_ref(r["author"], agent_map, user_map)
            if natural_author is None:
                log.warning(
                    "seed room_transcripts: unresolvable author=%s in room %s; skipping",
                    r["author"], r["room_id"],
                )
                continue
            dao.execute(
                "INSERT OR IGNORE INTO room_transcripts "
                "(id, room_id, author, kind, body, meta, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["room_id"], natural_author,
                    r["kind"] or "message", r["body"] or "",
                    r["meta"] or "{}", r["ts"],
                ),
                conn=conn,
            )
            counts["room_transcripts"] += 1

    # --- 7. embeddings (scopes-owned source_types: session/scope/room/project) -

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT source_type, source_id, chunk_text, embedding, updated_at "
            "FROM embeddings"
        ).fetchall():
            if r["source_type"] not in _SCOPES_EMBED_TYPES:
                continue
            dao.execute(
                "INSERT OR IGNORE INTO embeddings "
                "(source_type, source_id, chunk_text, embedding, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    r["source_type"], r["source_id"],
                    r["chunk_text"], r["embedding"], r["updated_at"],
                ),
                conn=conn,
            )
            counts["embeddings"] += 1

    return counts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    legacy = sys.argv[1] if len(sys.argv) > 1 else None
    counts = seed_from_legacy(legacy)
    print("Seed complete:")
    for table, n in counts.items():
        print(f"  {table}: {n} rows seeded")


if __name__ == "__main__":
    main()
