"""One-time seed: carry all scopes-owned rows from the legacy shared
``state.db`` into the scopes service's own DB.

The modular cutover drops the shared runtime DB but leaves the old file on
disk as a read-only legacy source. This module extracts the identity tables
(projects/users/agents) straight, then folds the old comms pair
(``session_logs`` + ``messages``) into the unified ``scope_posts`` channel.

A scope IS the channel, so:
  - ``session_logs``    → ``scope_posts`` with ``kind='journal'`` (a self-post by
    the owning agent; the structured debrief fields move into ``meta``; the
    open/resolved lifecycle is dropped).
  - ``messages``        → ``scope_posts`` with ``kind='message'`` (read/unread
    state dropped). The recipient becomes the channel owner; non-agent
    recipients (user/project/system) become NON-LITERAL channels
    (owner_project='', the ref kept verbatim in owner_scope).

Re-keying rules (per SCHEMA_HANDOFF.md):
  - ``agent_id`` → ``(project, scope)`` via the identity join.
  - Polymorphic refs (uuid | 'system') → natural-key strings:
      agent uuid → 'agent:<project>/<scope>';  user uuid → 'user:<username>';
      'system'/'' → 'system'.

Unresolvable rows are SKIPPED with a loud log line; no orphans are written.
Idempotent: derived stable PKs + ``INSERT OR IGNORE``, so re-running is safe.

Usage:
    python -m awm.scopes.seed [LEGACY_STATE_DB]   # defaults to awm.config.DB_PATH
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

from awm.scopes.dao import ScopesDAO, init

log = logging.getLogger("awm.scopes.seed")

# Embeddings source_types still seeded verbatim. 'session' is dropped: its
# source_ids point at the old session_logs ids that no longer exist as such.
# Post-level search re-indexes lazily on first sync.
_SCOPES_EMBED_TYPES = {"scope", "project"}


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
    natural-key ref string, or None if unresolvable."""
    if ref_id == "system" or not ref_id:
        return "system"
    if ref_id in agent_map:
        proj, sc = agent_map[ref_id]
        return f"agent:{proj}/{sc}"
    if ref_id in user_map:
        return f"user:{user_map[ref_id]}"
    return None


def _ref_to_owner(ref: str) -> tuple[str, str]:
    """Map a natural-key ref to a channel owner (owner_project, owner_scope).

    A literal worktree scope keys on (project, scope). Everything else is a
    NON-LITERAL channel: owner_project='' and the ref kept verbatim in
    owner_scope, so it round-trips.
    """
    if ref.startswith("agent:"):
        body = ref[len("agent:"):]
        project, _, scope = body.partition("/")
        if project and scope:
            return (project, scope)
        return ("", ref)
    if ref in ("system", "", "workspace"):
        return ("", "workspace")
    # 'user:<name>', 'project:<name>', or any other literal → non-literal channel.
    return ("", ref)


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
        "scope_posts": 0,
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
            "SELECT id, project_id, scope, status, agent_cli, branch, "
            "worktree, display_name, is_vagrant, created_at, retired_at FROM agents"
        ).fetchall():
            dao.execute(
                "INSERT OR IGNORE INTO agents "
                "(id, project_id, scope, status, agent_cli, branch, "
                " worktree, display_name, is_vagrant, created_at, retired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r["project_id"], r["scope"],
                    r["status"], r["agent_cli"] or "claude", r["branch"] or "",
                    r["worktree"] or "", r["display_name"] or r["scope"],
                    r["is_vagrant"] or 0, r["created_at"], r["retired_at"],
                ),
                conn=conn,
            )
            counts["agents"] += 1

    # Build lookup maps AFTER identity tables are seeded (join on legacy data).
    agent_map = _build_agent_map(src)
    user_map = _build_user_map(src)

    # --- 2. session_logs → scope_posts (kind='journal', self-post) ----------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT id, agent_id, created_at, file_path, git_commit, summary, "
            "metadata, content, skill_path, outcome, deviations, suggestions, "
            "skill_version, title FROM session_logs"
        ).fetchall():
            ps = agent_map.get(r["agent_id"])
            if ps is None:
                log.warning(
                    "seed session_logs: unresolvable agent_id=%s; skipping row",
                    r["agent_id"],
                )
                continue
            project, scope = ps
            meta: dict = {}
            if r["metadata"]:
                try:
                    meta = json.loads(r["metadata"])
                except (TypeError, ValueError):
                    meta = {"metadata_raw": r["metadata"]}
            for k in ("title", "skill_path", "skill_version", "outcome",
                      "deviations", "suggestions", "file_path", "git_commit",
                      "content"):
                if r[k]:
                    meta[k] = r[k]
            dao.execute(
                "INSERT OR IGNORE INTO scope_posts "
                "(id, owner_project, owner_scope, author, kind, body, meta, ts) "
                "VALUES (?, ?, ?, ?, 'journal', ?, ?, ?)",
                (
                    f"journal:{r['id']}", project, scope,
                    f"agent:{project}/{scope}", r["summary"] or "",
                    json.dumps(meta), r["created_at"],
                ),
                conn=conn,
            )
            counts["scope_posts"] += 1

    # --- 3. messages → scope_posts (kind='message') -------------------------

    with dao.transaction() as conn:
        for r in src.execute(
            "SELECT id, recipient_id, sender_id, msg_type, subject, body, "
            "metadata, created_at FROM messages"
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
            owner_project, owner_scope = _ref_to_owner(rec_ref)
            meta: dict = {}
            if r["metadata"]:
                try:
                    meta = json.loads(r["metadata"])
                except (TypeError, ValueError):
                    meta = {"metadata_raw": r["metadata"]}
            if r["subject"]:
                meta["subject"] = r["subject"]
            if r["msg_type"]:
                meta["msg_type"] = r["msg_type"]
            dao.execute(
                "INSERT OR IGNORE INTO scope_posts "
                "(id, owner_project, owner_scope, author, kind, body, meta, ts) "
                "VALUES (?, ?, ?, ?, 'message', ?, ?, ?)",
                (
                    f"msg:{r['id']}", owner_project, owner_scope, send_ref,
                    r["body"] or "", json.dumps(meta), r["created_at"],
                ),
                conn=conn,
            )
            counts["scope_posts"] += 1

    # --- 4. embeddings (identity-level source_types only) -------------------

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
