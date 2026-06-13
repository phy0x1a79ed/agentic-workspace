# Persistence schema handoff (T1 → per-feature fanout / T4)

This doc is the contract for the per-feature fanout agents. The `persistence`
component now ships a **per-service DB factory** — every feature service stands
up its OWN SQLite DB at `AWM_DIR/services/<service>/<service>.db` and owns its
own tables and `schema_version`. There is **no shared runtime `state.db`**; the
old single-file DB survives on disk only as a **read-only legacy seed source**.

Each service, at startup, calls:

```python
from awm.persistence.databases import init_service_db, get_connection
init_service_db("<service>", SCHEMA_SQL, schema_version=1, migrations=None)
```

`SCHEMA_SQL` is the service's OWN tables only, re-keyed to natural keys (see
below). A DAO subclasses `awm.persistence.dao.BaseDAO("<service>")`.

## How re-keying works (read this first)

The legacy v37 schema centred every data row on a uuid `agent_id` that
`REFERENCES agents(id)`, with human identity (`project`, `scope`, `username`)
living on the identity tables (`projects`/`agents`/`users`). Under per-service
DBs there is **no global identity table to FK against** — `agents`/`projects`
live in the `scopes` service's DB, unreachable from another service's DB.

So the per-service v1 schemas drop cross-DB FKs and **carry the natural key
inline**: any column that was `agent_id TEXT REFERENCES agents(id)` becomes
`project TEXT, scope TEXT`. Within-service FKs (e.g. `guest_list.room_id →
rooms.id`, both owned by `scopes`) are kept. Refs that cross a service boundary
become plain natural-key strings, validated at the app layer by calling the
owning service over gateway RPC (cached).

### Seeding from legacy `state.db`

When a service seeds its v1 DB from the legacy file it SELECTs the **v37 (=v39)
legacy shapes** documented per-service below. Because legacy data rows carry
only `agent_id`, the seed code must resolve `agent_id → (project, scope)` via
the legacy identity join:

```sql
-- legacy state.db: agent_id → (project, scope)
SELECT a.id AS agent_id, p.name AS project, a.scope AS scope
  FROM agents a
  JOIN projects p ON p.id = a.project_id;
```

Polymorphic refs (`messages.recipient_id` / `messages.sender_id`,
`room_transcripts.author`) hold an `agents.id`, a `users.id`, or the literal
`'system'`. Resolve agent uuids via the join above; resolve `users.id` via
`SELECT username FROM users WHERE id=?`; pass `'system'` through.

`*_at` columns in legacy are INTEGER unix-ms.

## The `embeddings` table is per-service

Any service that does semantic search gets its OWN copy of the tiny embeddings
table. The DDL is exported as `awm.persistence.embeddings.EMBEDDINGS_DDL` —
register it alongside that service's own tables in its `init_service_db` call.
The engine functions (`embed_text`, `upsert_embedding(conn, …)`,
`semantic_search(conn, …)`, `hybrid_augment(conn, …)`, `delete_embedding(conn,
…)`) all take the service's own connection. Services known to index today:
`skills` (skill files), and any of `scopes`/`artifacts` that index sessions /
scopes / rooms / projects / artifacts (those feature-specific indexers were
removed from `persistence` and must be reimplemented per-service against the
owning service's DAO + its own embeddings table).

`embeddings` v1 DDL (identical to legacy — seed is a straight copy):

```sql
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);
```

Legacy `state.db` rows to seed from: `SELECT source_type, source_id,
chunk_text, embedding, updated_at FROM embeddings` — but filter by the
`source_type` the owning service is responsible for (`'skill'` for skills,
`'session'`/`'scope'`/`'room'`/`'project'` for scopes, `'artifact'` for
artifacts).

---

# Ownership map

| Service | Owns (v1 tables) |
|---|---|
| **scopes** | projects, users, agents, agent identity layer, session_logs, messages, rooms, guest_list, room_transcripts (+ embeddings for session/scope/room/project) |
| **agents** | agent_instances, agent_transcript |
| **artifacts** | artifacts (+ embeddings for artifact) |
| **skills** | embeddings (skill) only — catalog is file-based |
| **discord** | discord_operators |
| **config** | config (KV) — already wired by T1 |

> Note: in legacy v37, `agent_instances` lived in the same DB as `agents`. The
> plan assigns `agent_instances` + `agent_transcript` to the **agents** service.
> Since `agent_instances.agent_id` referenced `agents(id)` (a `scopes`-owned
> table), that FK crosses the DB boundary and is re-keyed to `project, scope`
> below. `agent_transcript` has no legacy table (the legacy room/agent
> transcript was `room_transcripts`, owned by scopes); it is a new agents-owned
> table — seed source is empty, define fresh.

---

# scopes

Owns the identity layer plus comms/sessions/rooms. Within-service FKs are kept
(everything below lives in one DB), so identity stays relational here — this is
the one service that retains `agents`/`projects`/`users`.

## v1 schema (scopes.db)

```sql
-- Identity (kept relational — all in the scopes DB) -------------------
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT,
    repo_path   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    scope         TEXT NOT NULL,
    parent_id     TEXT REFERENCES agents(id),
    status        TEXT NOT NULL,
    agent_cli     TEXT NOT NULL,
    branch        TEXT NOT NULL,
    worktree      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    is_vagrant    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    retired_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agents_project_status ON agents(project_id, status);
CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_id);

-- Sessions ------------------------------------------------------------
-- agent_id FK dropped; (project, scope) carried inline.
CREATE TABLE IF NOT EXISTS session_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    scope         TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    file_path     TEXT NOT NULL DEFAULT '',
    git_commit    TEXT,
    summary       TEXT NOT NULL DEFAULT '',
    metadata      TEXT,
    content       TEXT,
    skill_path    TEXT,
    outcome       TEXT,
    deviations    TEXT,
    suggestions   TEXT,
    skill_version TEXT,
    resolved_at   INTEGER,
    resolution    TEXT,
    title         TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_logs_scope ON session_logs(project, scope, created_at DESC);

-- Messaging -----------------------------------------------------------
-- recipient_id/sender_id were polymorphic refs (agent uuid | user uuid |
-- 'system'). Re-key to natural-key ref strings: store the literal the app
-- layer resolves, e.g. 'agent:<project>/<scope>', 'user:<name>', 'system'.
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_ref TEXT NOT NULL,
    sender_ref    TEXT NOT NULL,
    msg_type      TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    metadata      TEXT,
    status        TEXT NOT NULL DEFAULT 'unread',
    created_at    INTEGER NOT NULL,
    read_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
    ON messages(recipient_ref, status, created_at DESC);

-- Rooms (within-service FKs kept) -------------------------------------
-- owner_agent_id was an agents(id) FK; agents is in this same DB, but the
-- room owner is best addressed by natural key going forward → owner_project,
-- owner_scope. (A scope IS the channel; one owner agent.)
CREATE TABLE IF NOT EXISTS rooms (
    id              TEXT PRIMARY KEY,
    owner_project   TEXT NOT NULL,
    owner_scope     TEXT NOT NULL,
    topic           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    closed_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rooms_owner ON rooms(owner_project, owner_scope);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);

CREATE TABLE IF NOT EXISTS guest_list (
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    guest_kind     TEXT NOT NULL,       -- 'agent' | 'user'
    guest_ref      TEXT NOT NULL,       -- natural key: 'project/scope' or 'user:<name>'
    display_name   TEXT NOT NULL,
    subscriptions  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (room_id, guest_kind, guest_ref)
);

CREATE TABLE IF NOT EXISTS room_transcripts (
    id       TEXT PRIMARY KEY,
    room_id  TEXT NOT NULL REFERENCES rooms(id),
    author   TEXT NOT NULL,             -- natural-key ref string or 'system'
    kind     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_room_ts ON room_transcripts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_kind ON room_transcripts(room_id, kind, ts);
```

Plus `embeddings` (see top) if scopes indexes sessions/scopes/rooms/projects.

## Legacy `state.db` shapes scopes SELECTs when seeding

```sql
-- identity (also used to resolve agent_id → project/scope everywhere)
SELECT id, name, url, repo_path, created_at FROM projects;
SELECT id, username FROM users;
SELECT id, project_id, scope, parent_id, status, agent_cli, branch,
       worktree, display_name, is_vagrant, created_at, retired_at FROM agents;

-- session_logs: legacy carries agent_id; join to (project, scope)
SELECT agent_id, created_at, file_path, git_commit, summary, metadata, content,
       skill_path, outcome, deviations, suggestions, skill_version,
       resolved_at, resolution, title FROM session_logs;

-- messages: recipient_id/sender_id are polymorphic refs (resolve per top note)
SELECT recipient_id, sender_id, msg_type, subject, body, metadata, status,
       created_at, read_at FROM messages;

-- rooms: owner_agent_id → resolve to (owner_project, owner_scope)
SELECT id, owner_agent_id, topic, status, created_at, closed_at FROM rooms;

-- guest_list: guest_ref is agent uuid or user uuid (resolve to natural key)
SELECT room_id, guest_kind, guest_ref, display_name, subscriptions FROM guest_list;

-- room_transcripts: author is a polymorphic ref (resolve per top note)
SELECT id, room_id, author, kind, body, meta, ts FROM room_transcripts;
```

---

# agents

## v1 schema (agents.db)

```sql
-- agent_instances: legacy agent_id → agents(id) was a scopes-owned FK; drop
-- it and carry (project, scope) inline.
CREATE TABLE IF NOT EXISTS agent_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    scope           TEXT NOT NULL,
    cli_session_id  TEXT,
    log_path        TEXT,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    data            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agent_instances_scope_started
    ON agent_instances(project, scope, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_instances_open
    ON agent_instances(project, scope) WHERE ended_at IS NULL;

-- agent_transcript: NEW agents-owned table (no legacy source). Define to the
-- agents service's needs; suggested shape mirrors room_transcripts.
CREATE TABLE IF NOT EXISTS agent_transcript (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_transcript_scope_ts
    ON agent_transcript(project, scope, ts);
```

## Legacy `state.db` shapes agents SELECTs when seeding

```sql
-- agent_instances: legacy agent_id → resolve to (project, scope) via the
-- identity join at the top of this doc.
SELECT agent_id, cli_session_id, log_path, started_at, ended_at, data
  FROM agent_instances;
```

`agent_transcript` has no legacy seed source — start empty.

---

# artifacts

## v1 schema (artifacts.db)

```sql
-- agent_id FK dropped; (project, scope) carried inline.
CREATE TABLE IF NOT EXISTS artifacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL,
    scope          TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    artifact_type  TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL DEFAULT '',
    description    TEXT,
    format         TEXT,
    tags           TEXT,
    status         TEXT NOT NULL DEFAULT 'current',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(project, scope, created_at DESC);
```

Plus `embeddings` (see top), `source_type='artifact'`.

## Legacy `state.db` shapes artifacts SELECTs when seeding

```sql
-- legacy carries agent_id; join to (project, scope)
SELECT agent_id, name, artifact_type, path, description, format, tags,
       status, created_at, updated_at FROM artifacts;
```

---

# skills

The skill catalog is **file-based** (`skills/awm/`, `skills/tools/`); skills
owns no relational state in the legacy DB beyond its embeddings rows. v1 DB only
needs the `embeddings` table.

## v1 schema (skills.db)

Just the `embeddings` table (see top), `source_type='skill'`.

## Legacy `state.db` shapes skills SELECTs when seeding

```sql
SELECT source_type, source_id, chunk_text, embedding, updated_at
  FROM embeddings WHERE source_type = 'skill';
```

---

# discord

## v1 schema (discord.db)

```sql
-- origin_peer dropped (federation retired).
CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    awm_user        TEXT NOT NULL DEFAULT '',
    added_at        TEXT NOT NULL DEFAULT ''
);
```

## Legacy `state.db` shapes discord SELECTs when seeding

```sql
SELECT discord_user_id, awm_user, added_at FROM discord_operators;
```

`awm_user` is a username literal — keep as-is (no uuid resolution needed; it was
already a plain username string in legacy).

---

# config

Already wired by T1. The `config` service owns the KV `config` table in
`config.db`; `awm.persistence.config_service` registers it via
`init_service_db("config", CONFIG_TABLE_DDL, schema_version=1)`.

## v1 schema (config.db)

```sql
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

## Legacy `state.db` shapes config SELECTs when seeding

```sql
SELECT key, value, updated_at FROM config;
```
