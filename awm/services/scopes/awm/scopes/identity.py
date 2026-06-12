"""Identity translation: ``(project, scope)`` ↔ ``agent_id`` and friends.

v37 swung the data tables onto uuid primary keys (projects/users/agents) so
renaming a project, scope, or username is a one-row UPDATE — but the CLI,
MCP, and exposed surfaces still address agents by their human-facing
``(project, scope)`` pair. This module is the single place where the
boundary code translates between the two.

All lookups are read-only fast-paths over the agents/projects/users tables.
Per-call sqlite queries are cheap (indexed, in-process); a tiny LRU keeps
the hot ``(project, scope) → agent_id`` lookup off disk in the common case
where the daemon resolves the same scope dozens of times per second.
"""

from __future__ import annotations

import functools
import sqlite3
import uuid as _uuid
from datetime import datetime, timezone

from awm.db import SENTINEL_USER_IDS, get_connection

SYSTEM_REF = "system"


# ---------- timestamp helpers ----------------------------------------------


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_to_ms(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    s = iso_str.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int | None) -> str | None:
    """unix-ms → ISO-8601 UTC string. Used to keep legacy API responses
    (which expose TEXT timestamps) stable while the underlying columns are
    INTEGER."""
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat()


# ---------- project lookups -------------------------------------------------


def project_by_name(name: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """Return the projects row for ``name`` or None."""
    own = conn is None
    c = conn or get_connection()
    try:
        row = c.execute(
            "SELECT id, name, url, repo_path, created_at FROM projects WHERE name=?",
            (name,),
        ).fetchone()
    finally:
        if own:
            c.close()
    return dict(row) if row else None


def project_id_for_name(name: str, *, conn: sqlite3.Connection | None = None) -> str | None:
    p = project_by_name(name, conn=conn)
    return p["id"] if p else None


def ensure_project(name: str, *, repo_path: str, url: str | None = None,
                   conn: sqlite3.Connection | None = None) -> str:
    """Idempotently get-or-create a projects row. Returns the project id.

    The DDL uses uuid4 PKs for fresh rows; ``url`` is set on insert and
    preserved on subsequent calls (a follow-up UPDATE can change it
    explicitly).
    """
    own = conn is None
    c = conn or get_connection()
    try:
        row = c.execute(
            "SELECT id FROM projects WHERE name=?", (name,),
        ).fetchone()
        if row:
            return row[0]
        pid = str(_uuid.uuid4())
        c.execute(
            "INSERT INTO projects (id, name, url, repo_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, name, url, repo_path, now_ms()),
        )
        if own:
            c.commit()
        return pid
    finally:
        if own:
            c.close()


# ---------- agent lookups ---------------------------------------------------


def agent_by_id(agent_id: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    c = conn or get_connection()
    try:
        row = c.execute(
            "SELECT a.*, p.name AS project_name "
            "FROM agents a JOIN projects p ON p.id = a.project_id "
            "WHERE a.id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        if own:
            c.close()
    return dict(row) if row else None


def agent_id_for_scope(project: str, scope: str, *,
                       conn: sqlite3.Connection | None = None,
                       active_only: bool = True) -> str | None:
    """Return the agent id for ``(project, scope)``. By default returns only
    a live (status ∈ allocated/active) agent; pass ``active_only=False`` to
    fall through to the most recently retired agent for the same name."""
    own = conn is None
    c = conn or get_connection()
    try:
        statuses = ("allocated", "active") if active_only else ("allocated", "active", "retired")
        placeholders = ", ".join("?" * len(statuses))
        row = c.execute(
            f"SELECT a.id FROM agents a "
            f"JOIN projects p ON p.id = a.project_id "
            f"WHERE p.name=? AND a.scope=? AND a.status IN ({placeholders}) "
            f"ORDER BY a.created_at DESC LIMIT 1",
            (project, scope, *statuses),
        ).fetchone()
    finally:
        if own:
            c.close()
    return row[0] if row else None


def agent_record_for_scope(project: str, scope: str, *,
                           conn: sqlite3.Connection | None = None,
                           active_only: bool = True) -> dict | None:
    aid = agent_id_for_scope(project, scope, conn=conn, active_only=active_only)
    return agent_by_id(aid, conn=conn) if aid else None


def project_scope_for_agent(agent_id: str, *,
                            conn: sqlite3.Connection | None = None
                            ) -> tuple[str, str] | None:
    """Inverse lookup — used by every place that still wants to render
    ``project/scope`` for an agent uuid (history.md, room labels, …)."""
    a = agent_by_id(agent_id, conn=conn)
    if not a:
        return None
    return (a["project_name"], a["scope"])


def ensure_agent(project: str, scope: str, *,
                 branch: str | None = None,
                 worktree: str = "",
                 agent_cli: str = "claude",
                 status: str = "allocated",
                 is_vagrant: bool = False,
                 display_name: str | None = None,
                 conn: sqlite3.Connection | None = None) -> str:
    """Get-or-create an agents row for ``(project, scope)``. Returns the
    agent id. Requires the project to exist already (raise KeyError if not).
    """
    own = conn is None
    c = conn or get_connection()
    try:
        # Already-live agent for this name wins.
        existing = agent_id_for_scope(project, scope, conn=c, active_only=True)
        if existing:
            return existing
        pid = project_id_for_name(project, conn=c)
        if not pid:
            raise KeyError(f"project {project!r} not found — call ensure_project first")
        aid = str(_uuid.uuid4())
        c.execute(
            "INSERT INTO agents "
            "(id, project_id, scope, parent_id, status, agent_cli, branch, "
            " worktree, display_name, is_vagrant, created_at, retired_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (aid, pid, scope, status, agent_cli,
             branch or f"feat/{scope}", worktree,
             display_name or scope, 1 if is_vagrant else 0, now_ms()),
        )
        if own:
            c.commit()
        return aid
    finally:
        if own:
            c.close()


def retire_agent(agent_id: str, *, conn: sqlite3.Connection | None = None) -> None:
    """Mark an agent retired (idempotent). Sets retired_at if not already."""
    own = conn is None
    c = conn or get_connection()
    try:
        c.execute(
            "UPDATE agents SET status='retired', "
            "retired_at = COALESCE(retired_at, ?) WHERE id=?",
            (now_ms(), agent_id),
        )
        if own:
            c.commit()
    finally:
        if own:
            c.close()


# ---------- user lookups ----------------------------------------------------


def user_id_for_username(username: str, *,
                         conn: sqlite3.Connection | None = None,
                         create_if_missing: bool = False) -> str | None:
    own = conn is None
    c = conn or get_connection()
    try:
        row = c.execute(
            "SELECT id FROM users WHERE username=?", (username,),
        ).fetchone()
        if row:
            return row[0]
        if not create_if_missing:
            return None
        uid = str(_uuid.uuid4())
        c.execute(
            "INSERT INTO users (id, username) VALUES (?, ?)",
            (uid, username),
        )
        if own:
            c.commit()
        return uid
    finally:
        if own:
            c.close()


def username_for_user_id(user_id: str, *,
                         conn: sqlite3.Connection | None = None) -> str | None:
    own = conn is None
    c = conn or get_connection()
    try:
        row = c.execute(
            "SELECT username FROM users WHERE id=?", (user_id,),
        ).fetchone()
    finally:
        if own:
            c.close()
    return row[0] if row else None


# ---------- polymorphic ref helpers ----------------------------------------


def resolve_ref(literal: str, *,
                conn: sqlite3.Connection | None = None,
                create_users: bool = False) -> str | None:
    """Resolve a literal author/sender/recipient string to a target id.

    Recognized shapes:
      - 'agent:<project>/<scope>' → agents.id
      - 'scope:<project>/<scope>' → agents.id
      - '<project>/<scope>' → agents.id
      - 'user:<name>' → users.id (insert when ``create_users``)
      - 'system' / '' → 'system' sentinel string

    Returns ``None`` when nothing resolves and the caller didn't opt into
    user-on-demand creation; callers can treat that as "fall back to the
    system sentinel" or surface a 400.
    """
    if not literal or literal == "system":
        return SYSTEM_REF
    if literal.startswith("agent:"):
        rest = literal[len("agent:"):]
        if "/" in rest:
            proj, sc = rest.split("/", 1)
            return agent_id_for_scope(proj, sc, conn=conn, active_only=False)
        return None
    if literal.startswith("scope:"):
        rest = literal[len("scope:"):]
        if "/" in rest:
            proj, sc = rest.split("/", 1)
            return agent_id_for_scope(proj, sc, conn=conn, active_only=False)
        return None
    if literal.startswith("user:"):
        name = literal[len("user:"):]
        return user_id_for_username(name, conn=conn, create_if_missing=create_users)
    if "/" in literal:
        proj, sc = literal.split("/", 1)
        return agent_id_for_scope(proj, sc, conn=conn, active_only=False)
    # Bare opaque literal — treat as a username.
    return user_id_for_username(literal, conn=conn, create_if_missing=create_users)


def display_for_ref(ref: str, *,
                    conn: sqlite3.Connection | None = None) -> str:
    """Reverse: ``ref`` (agent uuid | user uuid | 'system') → human label
    for log lines / UI. Falls back to the raw ref if unresolvable."""
    if ref == SYSTEM_REF or not ref:
        return "system"
    own = conn is None
    c = conn or get_connection()
    try:
        # Agents: render as ``project/scope`` (legible identity that survives
        # rename via the JOIN to projects).
        row = c.execute(
            "SELECT p.name, a.scope FROM agents a "
            "JOIN projects p ON p.id = a.project_id WHERE a.id=?",
            (ref,),
        ).fetchone()
        if row:
            return f"{row[0]}/{row[1]}"
        row = c.execute("SELECT username FROM users WHERE id=?", (ref,)).fetchone()
        if row:
            return row[0]
    finally:
        if own:
            c.close()
    return ref


# ---------- sentinel users (re-exported for convenience) -------------------


CLI_USER_ID = SENTINEL_USER_IDS["cli"]
SYSTEM_USER_ID = SENTINEL_USER_IDS["system"]


__all__ = [
    "SYSTEM_REF", "CLI_USER_ID", "SYSTEM_USER_ID",
    "now_ms", "iso_to_ms", "ms_to_iso",
    "project_by_name", "project_id_for_name", "ensure_project",
    "agent_by_id", "agent_id_for_scope", "agent_record_for_scope",
    "project_scope_for_agent", "ensure_agent", "retire_agent",
    "user_id_for_username", "username_for_user_id",
    "resolve_ref", "display_for_ref",
]
