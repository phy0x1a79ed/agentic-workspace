"""Identity translation: ``(project, scope)`` ↔ ``agent_id`` and friends.

Rewritten off the shared ``state.db`` onto :class:`ScopesDAO`: every lookup
goes to the scopes service's own per-service DB
(``AWM_DIR/services/scopes/scopes.db``).

The module surface (helper names, signatures, constants) is STABLE — the
surfaces agent imports these unchanged. Internally we still use uuid PKs for
agents/projects/users; those uuids never leave this module over the RPC
boundary (the four identity RPCs in ``hub_adapter.py`` map to/from natural
keys at the manifest edge).

All helpers follow the own-or-passed-connection idiom via
``ScopesDAO(conn=...)`` so a caller can wrap multiple operations in one
transaction.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from awm.scopes.dao import ScopesDAO

# ---------- sentinel refs --------------------------------------------------

SYSTEM_REF = "system"

# Deterministic sentinel user IDs (same derivation as the legacy ``db.py``
# so seed data that carries these uuids keeps resolving correctly).
import uuid as _uuid_mod

_SENTINEL_NS = _uuid_mod.uuid5(_uuid_mod.NAMESPACE_DNS, "awm.v37.users.sentinels")
CLI_USER_ID = str(_uuid_mod.uuid5(_SENTINEL_NS, "cli"))
SYSTEM_USER_ID = str(_uuid_mod.uuid5(_SENTINEL_NS, "system"))


# ---------- timestamp helpers -----------------------------------------------


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
    """unix-ms → ISO-8601 UTC string. Keeps legacy API responses stable."""
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat()


# ---------- project lookups -------------------------------------------------


def project_by_name(name: str, *, conn=None) -> dict | None:
    """Return the projects row for ``name`` or None."""
    dao = ScopesDAO(conn=conn)
    return dao.query_one(
        "SELECT id, name, url, repo_path, created_at FROM projects WHERE name=?",
        (name,),
    )


def project_id_for_name(name: str, *, conn=None) -> str | None:
    p = project_by_name(name, conn=conn)
    return p["id"] if p else None


def ensure_project(name: str, *, repo_path: str, url: str | None = None,
                   conn=None) -> str:
    """Idempotently get-or-create a projects row. Returns the project id.

    The DDL uses uuid4 PKs for fresh rows; ``url`` is set on insert and
    preserved on subsequent calls.
    """
    dao = ScopesDAO(conn=conn)
    row = dao.query_one("SELECT id FROM projects WHERE name=?", (name,))
    if row:
        return row["id"]
    pid = str(_uuid.uuid4())
    dao.execute(
        "INSERT INTO projects (id, name, url, repo_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, name, url, repo_path, now_ms()),
    )
    return pid


# ---------- agent lookups ---------------------------------------------------


def agent_by_id(agent_id: str, *, conn=None) -> dict | None:
    dao = ScopesDAO(conn=conn)
    return dao.query_one(
        "SELECT a.*, p.name AS project_name "
        "FROM agents a JOIN projects p ON p.id = a.project_id "
        "WHERE a.id = ?",
        (agent_id,),
    )


def agent_id_for_scope(project: str, scope: str, *,
                       conn=None,
                       active_only: bool = True) -> str | None:
    """Return the agent id for ``(project, scope)``.

    By default returns only a live (status ∈ allocated/active) agent; pass
    ``active_only=False`` to fall through to the most recently retired agent
    for the same name.
    """
    statuses = ("allocated", "active") if active_only else ("allocated", "active", "retired")
    placeholders = ", ".join("?" * len(statuses))
    dao = ScopesDAO(conn=conn)
    row = dao.query_one(
        f"SELECT a.id FROM agents a "
        f"JOIN projects p ON p.id = a.project_id "
        f"WHERE p.name=? AND a.scope=? AND a.status IN ({placeholders}) "
        f"ORDER BY a.created_at DESC LIMIT 1",
        (project, scope, *statuses),
    )
    return row["id"] if row else None


def agent_record_for_scope(project: str, scope: str, *,
                           conn=None,
                           active_only: bool = True) -> dict | None:
    aid = agent_id_for_scope(project, scope, conn=conn, active_only=active_only)
    return agent_by_id(aid, conn=conn) if aid else None


def project_scope_for_agent(agent_id: str, *, conn=None) -> tuple[str, str] | None:
    """Inverse lookup — used internally where ``project/scope`` must be rendered."""
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
                 conn=None) -> str:
    """Get-or-create an agents row for ``(project, scope)``. Returns the
    agent id. Requires the project to exist already (raises KeyError if not).
    """
    dao = ScopesDAO(conn=conn)
    # Already-live agent for this name wins.
    existing = agent_id_for_scope(project, scope, conn=conn, active_only=True)
    if existing:
        return existing
    pid = project_id_for_name(project, conn=conn)
    if not pid:
        raise KeyError(f"project {project!r} not found — call ensure_project first")
    aid = str(_uuid.uuid4())
    dao.execute(
        "INSERT INTO agents "
        "(id, project_id, scope, status, agent_cli, branch, "
        " worktree, display_name, is_vagrant, created_at, retired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (aid, pid, scope, status, agent_cli,
         branch or f"feat/{scope}", worktree,
         display_name or scope, 1 if is_vagrant else 0, now_ms()),
    )
    return aid


def retire_agent(agent_id: str, *, conn=None) -> None:
    """Mark an agent retired (idempotent). Sets retired_at if not already."""
    dao = ScopesDAO(conn=conn)
    dao.execute(
        "UPDATE agents SET status='retired', "
        "retired_at = COALESCE(retired_at, ?) WHERE id=?",
        (now_ms(), agent_id),
    )


# ---------- user lookups ----------------------------------------------------


def user_id_for_username(username: str, *,
                         conn=None,
                         create_if_missing: bool = False) -> str | None:
    dao = ScopesDAO(conn=conn)
    row = dao.query_one("SELECT id FROM users WHERE username=?", (username,))
    if row:
        return row["id"]
    if not create_if_missing:
        return None
    uid = str(_uuid.uuid4())
    dao.execute("INSERT INTO users (id, username) VALUES (?, ?)", (uid, username))
    return uid


def username_for_user_id(user_id: str, *, conn=None) -> str | None:
    dao = ScopesDAO(conn=conn)
    row = dao.query_one("SELECT username FROM users WHERE id=?", (user_id,))
    return row["username"] if row else None


# ---------- polymorphic ref helpers ----------------------------------------


def resolve_ref(literal: str, *,
                conn=None,
                create_users: bool = False) -> str | None:
    """Resolve a literal author/sender/recipient string to a target id.

    Recognized shapes:
      - 'agent:<project>/<scope>' → agents.id
      - 'scope:<project>/<scope>' → agents.id
      - '<project>/<scope>' → agents.id
      - 'user:<name>' → users.id (insert when ``create_users``)
      - 'system' / '' → 'system' sentinel string
      - bare opaque literal (no '/') → username → users.id

    Returns ``None`` when nothing resolves and the caller didn't opt into
    user-on-demand creation.
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


def display_for_ref(ref: str, *, conn=None) -> str:
    """Reverse: ``ref`` (agent uuid | user uuid | 'system') → human label."""
    if ref == SYSTEM_REF or not ref:
        return "system"
    dao = ScopesDAO(conn=conn)
    # Agents: render as ``project/scope``.
    row = dao.query_one(
        "SELECT p.name, a.scope FROM agents a "
        "JOIN projects p ON p.id = a.project_id WHERE a.id=?",
        (ref,),
    )
    if row:
        return f"{row['name']}/{row['scope']}"
    row = dao.query_one("SELECT username FROM users WHERE id=?", (ref,))
    if row:
        return row["username"]
    return ref


__all__ = [
    "SYSTEM_REF", "CLI_USER_ID", "SYSTEM_USER_ID",
    "now_ms", "iso_to_ms", "ms_to_iso",
    "project_by_name", "project_id_for_name", "ensure_project",
    "agent_by_id", "agent_id_for_scope", "agent_record_for_scope",
    "project_scope_for_agent", "ensure_agent", "retire_agent",
    "user_id_for_username", "username_for_user_id",
    "resolve_ref", "display_for_ref",
]
