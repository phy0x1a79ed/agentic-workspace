"""v37 migration preflight: walk the v36 source DB and emit a deterministic
seed manifest at ``<db_dir>/migrations/v37-seed.json``.

The migration itself (db.py:_migrate_v37) reads this manifest via
``executemany`` rather than rebuilding identity inside SQL — it's easier to
test, easier to inspect, and lets us tombstone-resolve dangling references
in plain Python instead of correlated subqueries.

Re-runnable: identical source DB + identical preflight code → byte-identical
seed file (uuids are uuid5 derived from peer_id + table + natural_key).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION_TARGET = 37
SEED_SCHEMA_VERSION = 1  # manifest format version, bump on shape change

_TOMBSTONE_DISPLAY_PREFIX = "__tombstone_"


# ---------- helpers ---------------------------------------------------------


def _iso_to_ms(iso_str: str | None) -> int | None:
    """ISO-8601 (with or without TZ) → unix-ms. Returns None for empty/invalid."""
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _git_origin_url(bare_dir: Path) -> str | None:
    if not bare_dir.is_dir():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(bare_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _peer_id(conn: sqlite3.Connection) -> str:
    """Local peer id from the peers table self-row; '' if absent."""
    if not _table_exists(conn, "peers"):
        return ""
    row = conn.execute(
        "SELECT peer_id FROM peers WHERE ssh_alias='self' LIMIT 1"
    ).fetchone()
    return (row[0] if row else "") or ""


def _det_ns(peer_id: str) -> uuid.UUID:
    """Per-peer deterministic namespace root for uuid5 derivation."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"awm.v37.{peer_id or 'local'}")


def _det_uuid(ns: uuid.UUID, *parts: str) -> str:
    """Deterministic uuid string from a colon-joined key."""
    return str(uuid.uuid5(ns, ":".join(parts)))


# ---------- manifest shape --------------------------------------------------


@dataclass
class SeedManifest:
    schema_version: int = SEED_SCHEMA_VERSION
    target_version: int = SCHEMA_VERSION_TARGET
    source_db_path: str = ""
    peer_id: str = ""
    # name → {id, url, repo_path, created_at}
    projects: dict = field(default_factory=dict)
    # username → id
    users: dict = field(default_factory=dict)
    # "project/scope" → {id, project_id, scope, parent_id, status, agent_cli,
    #                    branch, worktree, display_name, is_vagrant,
    #                    created_at, retired_at}
    agents: dict = field(default_factory=dict)
    # tombstone agents for room_posts.author / messages.sender referencing
    # scopes that no longer exist. Same shape as agents entries; keyed by
    # "project/scope" but project_id may point at a fabricated project.
    tombstone_agents: dict = field(default_factory=dict)
    # source-row uuid → target agent_id. Lets the rebuild dance map
    # session_logs/artifacts (which carry project, scope strings, not the
    # source row's PK) onto the right agent.
    scope_to_agent: dict = field(default_factory=dict)
    # one entry per agent_sessions row.
    # {id (source agent_sessions.id), agent_id, cli_session_id, log_path,
    #  started_at, ended_at, data (JSON string)}
    agent_instances: list = field(default_factory=list)
    # v36 rooms.id → owner agent_id (tombstone if necessary)
    room_owner_map: dict = field(default_factory=dict)
    # literal author/sender/recipient string → target id (agent uuid, user
    # uuid, or the sentinel 'system').
    ref_resolution: dict = field(default_factory=dict)


# ---------- preflight stages -----------------------------------------------


def _seed_projects(conn: sqlite3.Connection, m: SeedManifest, ns: uuid.UUID) -> None:
    """Emit one projects row per distinct scopes.project. URL pulled from
    git origin; repo_path from scopes.repo_path (most recent non-empty)
    or PROJECTS_DIR / project / .bare."""
    from awm.config import PROJECTS_DIR

    if not _table_exists(conn, "scopes"):
        return
    rows = conn.execute(
        "SELECT project, MAX(repo_path) AS repo_path, MIN(created_at) AS created_at "
        "FROM scopes WHERE project!='' GROUP BY project"
    ).fetchall()
    for r in rows:
        name = r["project"]
        repo_path = r["repo_path"] or str(PROJECTS_DIR / name / ".bare")
        # Prefer the canonical workspace layout if the recorded path is stale.
        canonical = PROJECTS_DIR / name / ".bare"
        if canonical.is_dir():
            repo_path = str(canonical)
        bare = Path(repo_path)
        url = _git_origin_url(bare)
        created_ms = _iso_to_ms(r["created_at"]) or 0
        m.projects[name] = {
            "id": _det_uuid(ns, "projects", name),
            "url": url,
            "repo_path": repo_path,
            "created_at": created_ms,
        }


def _seed_users(conn: sqlite3.Connection, m: SeedManifest, ns: uuid.UUID) -> None:
    """Seed cli + system + every distinct ``user:<name>`` extracted from
    room_posts.author / messages.sender."""
    for sentinel in ("cli", "system"):
        m.users[sentinel] = _det_uuid(ns, "users", sentinel)

    seen: set[str] = set()

    def _collect(values: list[str]) -> None:
        for v in values:
            if not v:
                continue
            if v.startswith("user:"):
                seen.add(v[len("user:"):])

    if _table_exists(conn, "room_posts"):
        rows = conn.execute("SELECT DISTINCT author FROM room_posts").fetchall()
        _collect([row[0] for row in rows])
    if _table_exists(conn, "messages"):
        rows = conn.execute("SELECT DISTINCT sender FROM messages").fetchall()
        _collect([row[0] for row in rows])
        rows = conn.execute("SELECT DISTINCT scope FROM messages").fetchall()
        _collect([row[0] for row in rows])

    for name in sorted(seen):
        if name in m.users:
            continue
        m.users[name] = _det_uuid(ns, "users", name)


def _seed_agents(conn: sqlite3.Connection, m: SeedManifest, ns: uuid.UUID) -> None:
    """One agents row per (project, scope) — the latest scopes row provides
    the field values. scope_to_agent maps the source-row uuid onto the
    resulting agent id so callers backfilling session_logs/artifacts can
    resolve by their (project, scope) text columns to the most-recent agent.
    """
    if not _table_exists(conn, "scopes"):
        return
    # Group: latest by updated_at per (project, scope)
    rows = conn.execute(
        """
        SELECT s.uuid, s.project, s.scope, s.status, s.branch, s.worktree,
               s.repo_path, s.created_at, s.updated_at
          FROM scopes s
         WHERE s.project!='' AND s.scope!=''
         ORDER BY s.project, s.scope, s.updated_at DESC, s.created_at DESC
        """
    ).fetchall()

    chosen: dict[tuple[str, str], dict] = {}
    all_rows: list[dict] = []
    for r in rows:
        key = (r["project"], r["scope"])
        rec = dict(r)
        all_rows.append(rec)
        if key not in chosen:
            chosen[key] = rec  # first hit per key = most-recent row

    # Determine agent_cli + active-session presence per (project, scope) from
    # agent_sessions (latest by started_at wins for agent_cli).
    has_active_session: dict[tuple[str, str], bool] = {}
    cli_by_scope: dict[tuple[str, str], str] = {}
    if _table_exists(conn, "agent_sessions"):
        s_rows = conn.execute(
            """
            SELECT project, scope, status, agent_cli, started_at
              FROM agent_sessions
             ORDER BY started_at DESC
            """
        ).fetchall()
        for r in s_rows:
            key = (r["project"], r["scope"])
            if r["status"] in ("starting", "running", "stopping", "orphaned"):
                has_active_session[key] = True
            cli_by_scope.setdefault(key, r["agent_cli"] or "claude")

    is_vagrant_set: set[tuple[str, str]] = set()
    if _table_exists(conn, "agent_relationships"):
        ar_rows = conn.execute(
            "SELECT child_project, child_scope, is_vagrant FROM agent_relationships"
        ).fetchall()
        for r in ar_rows:
            if r["is_vagrant"]:
                is_vagrant_set.add((r["child_project"], r["child_scope"]))

    from awm.config import VAGRANT_PROJECT

    for (project, scope), r in sorted(chosen.items()):
        project_entry = m.projects.get(project)
        if project_entry is None:
            # A scopes row references a project name we haven't seeded.
            # Fabricate a project so the FK resolves.
            from awm.config import PROJECTS_DIR
            repo_path = r.get("repo_path") or str(PROJECTS_DIR / project / ".bare")
            m.projects[project] = {
                "id": _det_uuid(ns, "projects", project),
                "url": None,
                "repo_path": repo_path,
                "created_at": _iso_to_ms(r.get("created_at")) or 0,
            }
            project_entry = m.projects[project]

        status_src = r["status"]
        if has_active_session.get((project, scope)):
            status = "active"
        elif status_src == "active":
            status = "allocated"
        else:
            status = "retired"

        agent_cli = cli_by_scope.get((project, scope), "claude")
        is_vagrant = 1 if (project == VAGRANT_PROJECT or (project, scope) in is_vagrant_set) else 0

        agent_id = _det_uuid(ns, "agents", project, scope)
        created_ms = _iso_to_ms(r.get("created_at")) or 0
        retired_ms = None if status_src == "active" else _iso_to_ms(r.get("updated_at"))

        entry = {
            "id": agent_id,
            "project_id": project_entry["id"],
            "scope": scope,
            "parent_id": None,
            "status": status,
            "agent_cli": agent_cli,
            "branch": r.get("branch") or f"feat/{scope}",
            "worktree": r.get("worktree") or "",
            "display_name": scope,
            "is_vagrant": is_vagrant,
            "created_at": created_ms,
            "retired_at": retired_ms,
        }
        key = f"{project}/{scope}"
        m.agents[key] = entry

    # Map every source-row uuid to its consolidated agent_id so the rebuild
    # dance can resolve session_logs/artifacts by (project, scope) text.
    for r in all_rows:
        key = f"{r['project']}/{r['scope']}"
        agent_id = m.agents.get(key, {}).get("id")
        if agent_id:
            m.scope_to_agent[r["uuid"]] = agent_id


def _tombstone_agent(
    m: SeedManifest, ns: uuid.UUID, project: str, scope: str
) -> str:
    """Insert (or look up) a tombstone agents row for an unknown (project, scope).

    The tombstone owns its own project entry if the project itself is missing.
    """
    key = f"{project}/{scope}"
    existing = m.tombstone_agents.get(key) or m.agents.get(key)
    if existing:
        return existing["id"]

    project_entry = m.projects.get(project)
    if project_entry is None:
        from awm.config import PROJECTS_DIR
        m.projects[project] = {
            "id": _det_uuid(ns, "projects", project),
            "url": None,
            "repo_path": str(PROJECTS_DIR / project / ".bare"),
            "created_at": 0,
        }
        project_entry = m.projects[project]

    agent_id = _det_uuid(ns, "agents", project, scope)
    m.tombstone_agents[key] = {
        "id": agent_id,
        "project_id": project_entry["id"],
        "scope": scope,
        "parent_id": None,
        "status": "retired",
        "agent_cli": "claude",
        "branch": f"feat/{scope}",
        "worktree": "",
        "display_name": f"{_TOMBSTONE_DISPLAY_PREFIX}{scope}__",
        "is_vagrant": 0,
        "created_at": 0,
        "retired_at": 0,
    }
    return agent_id


def _seed_agent_instances(conn: sqlite3.Connection, m: SeedManifest) -> None:
    """One agent_instances row per agent_sessions row. agent_id resolves via
    (project, scope) → m.agents (tombstone if missing)."""
    if not _table_exists(conn, "agent_sessions"):
        return
    rows = conn.execute(
        """
        SELECT id, project, scope, pid, status, agent_cli, started_at,
               exited_at, exit_code, log_path, claude_session_id, intent
          FROM agent_sessions
         ORDER BY id
        """
    ).fetchall()
    ns = _det_ns(m.peer_id)
    for r in rows:
        key = f"{r['project']}/{r['scope']}"
        agent_id = m.agents.get(key, {}).get("id")
        if not agent_id:
            agent_id = _tombstone_agent(m, ns, r["project"], r["scope"])
        started_ms = _iso_to_ms(r["started_at"]) or 0
        ended_ms = _iso_to_ms(r["exited_at"])
        data = {
            "intent": r["intent"] or "live",
            "exit_code": r["exit_code"],
            "prior_status": r["status"],
            "pid": r["pid"],
            "migrated_from_agent_sessions_id": r["id"],
        }
        m.agent_instances.append({
            "source_id": r["id"],
            "agent_id": agent_id,
            "cli_session_id": r["claude_session_id"],
            "log_path": r["log_path"],
            "started_at": started_ms,
            "ended_at": ended_ms,
            "data": json.dumps(data, sort_keys=True),
        })


def _seed_room_owner_map(conn: sqlite3.Connection, m: SeedManifest) -> None:
    """For each row in v36 rooms, resolve owner_scope_key → agent_id.

    Falls back to the unique active 'scope'-kind participant for legacy rooms
    with no owner_scope_key. Anything still unresolved gets a tombstone.
    """
    if not _table_exists(conn, "rooms"):
        return
    ns = _det_ns(m.peer_id)
    rows = conn.execute(
        "SELECT id, owner_scope_key FROM rooms"
    ).fetchall()
    for r in rows:
        room_id = r["id"]
        owner_key: str | None = r["owner_scope_key"]
        if not owner_key and _table_exists(conn, "room_participants"):
            # Heuristic: exactly one active scope participant
            cand = conn.execute(
                """
                SELECT identifier FROM room_participants
                 WHERE room_id=? AND kind='scope' AND left_at IS NULL
                """,
                (room_id,),
            ).fetchall()
            if len(cand) == 1:
                owner_key = cand[0]["identifier"]
        agent_id: str | None = None
        if owner_key and "/" in owner_key:
            project, scope = owner_key.split("/", 1)
            agent_id = m.agents.get(f"{project}/{scope}", {}).get("id")
            if not agent_id:
                agent_id = _tombstone_agent(m, ns, project, scope)
        if not agent_id:
            agent_id = _tombstone_agent(m, ns, "_unowned", room_id)
        m.room_owner_map[room_id] = agent_id


def _classify_literal(
    literal: str, m: SeedManifest, ns: uuid.UUID
) -> str:
    """Resolve a single polymorphic ref literal to a target id.

    Recognized shapes:
      - 'agent:<project>/<scope>' → agents.id (tombstone if missing)
      - 'scope:<project>/<scope>' → agents.id (messaging legacy form)
      - '<project>/<scope>' (bare path) → agents.id
      - 'user:<name>' → users.id (inserts if missing)
      - 'system' / 'workspace' → 'system' sentinel
      - 'project:<name>' → user with username ``proj:<name>``
        (auto-materialized so the project inbox FK-resolves)
      - anything else → users.id (literal stored as username, inserted on demand)
    """
    if literal == "system" or literal == "" or literal == "workspace":
        return "system"
    if literal.startswith("project:"):
        proj = literal[len("project:"):]
        username = f"proj:{proj}"
        if username not in m.users:
            m.users[username] = _det_uuid(ns, "users", username)
        return m.users[username]
    if literal.startswith("agent:"):
        rest = literal[len("agent:"):]
        if "/" in rest:
            project, scope = rest.split("/", 1)
            entry = m.agents.get(f"{project}/{scope}")
            if entry:
                return entry["id"]
            return _tombstone_agent(m, ns, project, scope)
        # bare agent:<name> with no project — tombstone under _unknown
        return _tombstone_agent(m, ns, "_unknown", rest)
    if literal.startswith("scope:"):
        rest = literal[len("scope:"):]
        if "/" in rest:
            project, scope = rest.split("/", 1)
            entry = m.agents.get(f"{project}/{scope}")
            if entry:
                return entry["id"]
            return _tombstone_agent(m, ns, project, scope)
        return _tombstone_agent(m, ns, "_unknown", rest)
    if literal.startswith("user:"):
        name = literal[len("user:"):]
        if name not in m.users:
            m.users[name] = _det_uuid(ns, "users", name)
        return m.users[name]
    if "/" in literal and not literal.startswith("_"):
        project, scope = literal.split("/", 1)
        entry = m.agents.get(f"{project}/{scope}")
        if entry:
            return entry["id"]
        return _tombstone_agent(m, ns, project, scope)
    # Catch-all: opaque literal → username
    if literal not in m.users:
        m.users[literal] = _det_uuid(ns, "users", literal)
    return m.users[literal]


def _seed_data_table_tombstones(
    conn: sqlite3.Connection, m: SeedManifest, ns: uuid.UUID
) -> None:
    """Ensure every (project, scope) referenced by session_logs / artifacts /
    messages resolves — tombstone any pair without an agents entry. This lets
    the rebuild dance lookup-only with no on-the-fly inserts."""
    pairs: set[tuple[str, str]] = set()
    if _table_exists(conn, "session_logs"):
        for r in conn.execute(
            "SELECT DISTINCT project, scope FROM session_logs WHERE project!='' AND scope!=''"
        ).fetchall():
            pairs.add((r[0], r[1]))
    if _table_exists(conn, "artifacts"):
        for r in conn.execute(
            "SELECT DISTINCT project, scope FROM artifacts WHERE project!='' AND scope!=''"
        ).fetchall():
            pairs.add((r[0], r[1]))
    if _table_exists(conn, "messages"):
        for r in conn.execute("SELECT DISTINCT scope FROM messages").fetchall():
            s = r[0]
            if s and "/" in s and not s.startswith("user:") and not s.startswith("agent:") and s != "system":
                parts = s.split("/", 1)
                pairs.add((parts[0], parts[1]))
    for project, scope in sorted(pairs):
        key = f"{project}/{scope}"
        if key in m.agents or key in m.tombstone_agents:
            continue
        _tombstone_agent(m, ns, project, scope)


def _seed_ref_resolution(conn: sqlite3.Connection, m: SeedManifest) -> None:
    """Pre-compute every distinct polymorphic-ref literal we'll encounter
    during backfill."""
    ns = _det_ns(m.peer_id)
    refs: set[str] = set()

    if _table_exists(conn, "room_posts"):
        for r in conn.execute("SELECT DISTINCT author FROM room_posts").fetchall():
            if r[0] is not None:
                refs.add(r[0])
    if _table_exists(conn, "messages"):
        for col in ("sender", "scope"):
            for r in conn.execute(f"SELECT DISTINCT {col} FROM messages").fetchall():
                if r[0] is not None:
                    refs.add(r[0])
    if _table_exists(conn, "room_participants"):
        for r in conn.execute(
            "SELECT DISTINCT kind, identifier FROM room_participants"
        ).fetchall():
            kind, ident = r["kind"], r["identifier"]
            if not ident:
                continue
            if kind == "scope":
                refs.add(ident)
            elif kind == "user":
                refs.add(f"user:{ident}")
            elif kind == "peer":
                # Peer participants don't have an agent identity — they map to
                # the user sentinel for now; can re-route once peer identity
                # lands as its own ref kind.
                refs.add(f"user:peer-{ident}")
            else:
                refs.add(ident)

    for literal in sorted(refs):
        m.ref_resolution[literal] = _classify_literal(literal, m, ns)


# ---------- entrypoint ------------------------------------------------------


def preflight(db_path: str | Path) -> Path:
    """Walk the v36 source DB at ``db_path`` and write the seed manifest to
    ``<db_dir>/migrations/v37-seed.json``. Returns the output path."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"source DB not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        m = SeedManifest()
        m.source_db_path = str(db_path)
        m.peer_id = _peer_id(conn)
        ns = _det_ns(m.peer_id)

        _seed_projects(conn, m, ns)
        _seed_users(conn, m, ns)
        _seed_agents(conn, m, ns)
        _seed_data_table_tombstones(conn, m, ns)
        _seed_agent_instances(conn, m)
        _seed_room_owner_map(conn, m)
        _seed_ref_resolution(conn, m)
    finally:
        conn.close()

    out_dir = db_path.parent / "migrations"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "v37-seed.json"
    out_path.write_text(json.dumps(asdict(m), indent=2, sort_keys=True) + "\n")
    return out_path


def load_manifest(seed_path: str | Path) -> SeedManifest:
    """Load + light-validate a seed manifest written by ``preflight``."""
    seed_path = Path(seed_path)
    data = json.loads(seed_path.read_text())
    if data.get("schema_version") != SEED_SCHEMA_VERSION:
        raise ValueError(
            f"seed manifest schema_version {data.get('schema_version')!r} != "
            f"{SEED_SCHEMA_VERSION}; regenerate via preflight"
        )
    if data.get("target_version") != SCHEMA_VERSION_TARGET:
        raise ValueError(
            f"seed manifest target_version {data.get('target_version')!r} != "
            f"{SCHEMA_VERSION_TARGET}"
        )
    return SeedManifest(**data)


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "preflight":
        sys.stderr.write(
            "usage: python -m awm.services.migration_v37 preflight <db_path>\n"
        )
        return 2
    out = preflight(argv[1])
    sys.stdout.write(f"{out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
