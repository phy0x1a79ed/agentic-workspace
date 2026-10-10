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

A service that searches keeps its own `embeddings` table and its own
`embeddings_fts` keyword index, in its own DB. The engine
`awm.persistence.embeddings` owns both tables. `ensure_schema(conn)` creates them,
and it upgrades the old one-row-per-item table in place. Every entry point calls
it, so no service carries its own migration.

Index through `index_document` and `reindex`. Search through `search`. Never
query the vectors directly. The contract, and why each part exists:

- **One row per chunk, over the whole item.** Structural chunks of at most 128
  MiniLM tokens cover the full body. A vector of the first slice only missed
  every detail past it.
- **A header on every chunk.** Each chunk embeds "title — project/scope · date —
  section" before its text. A chunk alone often lacks the words that say what
  it is about.
- **An item scores as its best chunk (MaxP).**
- **Filter before ranking.** `search(..., allowed=<SQL returning ids>)` scores
  only the chunks that pass. A global top-N cut followed by a filter returned
  nothing for any selective filter.
- **Keyword and meaning together.** BM25 over the same chunks is fused with
  cosine by a convex combination (`ALPHA`) after min-max scaling. The query
  reaches FTS5 as quoted terms, so punctuation cannot raise an error.
- **Rows record their model and a content hash.** `reindex` re-embeds what is
  missing, changed, or made by another model, and prunes what no longer exists.
  Each service runs it at startup and every few hours. Changing `MODEL_NAME` or
  the chunker therefore re-embeds everything with no manual step.
- **Degraded, not silent.** Without the semantic stack the engine stores
  keyword-only rows, and `search` returns a `degraded` block that callers pass
  through.

**CAUTION** The embedder is CPU-bound and loads once per process. Services run
indexing on one background thread, and bind the DB path when a job is queued. A
job that reads the path when it runs follows a test's monkeypatch back to the
production DB.

The choice of model, chunk size and fusion weight came from a labelled query
set. Re-measure with `awm/services/scopes/scripts/search_eval.py` before
changing any of them.

---

# Ownership map

| Service | Owns (v1 tables) |
|---|---|
| **scopes** | projects, users, agents, agent identity layer, session_logs, messages, rooms, guest_list, room_transcripts (+ embeddings for post/scope/project) |
| **skills** | embeddings (skill) only — catalog is file-based |
| **discord** | discord_operators |
| **config** | config (KV) — already wired by T1 |

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

Plus `embeddings` (see top) for posts, scopes and projects.

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
