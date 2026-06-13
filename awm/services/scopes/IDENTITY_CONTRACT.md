# scopes — identity RPC contract

The frozen contract for the identity functions the `scopes` service will
expose in its `ready.api` manifest. The T4 scopes agent **implements** these;
the `agents` / `artifacts` agents **code against this doc** (they do not import
`scopes`). This file is the agreement between them.

## Why this exists

In the monolith, any boundary code that needed to turn a human-facing
`(project, scope)` pair into an internal `agent_id` (and back) imported
`awm.scopes.identity` (formerly `awm.services.scopes.identity`) and queried the
shared `state.db` directly. That module is the thing being replaced.

The modular invariant: **no global identity; refs are natural keys validated
by calling the owning service via gateway RPC, cached.** `scopes` owns the
projects/agents/users tables in its own per-service DB. Every other service
that holds a reference to a scope validates it by *calling* `scopes` through
the gateway — `gatewayclient.call("scopes", "<fn>", {...}, as_=...)` — never by
importing it and never by touching `scopes`' DB.

## Cross-boundary rule: natural keys only, never uuids

`identity.py` swung the internal tables onto uuid PKs (`agents.id`,
`projects.id`, `users.id` are uuid4 strings). **Those uuids never cross the RPC
boundary.** Every function below takes and returns *natural keys*:

- a **project** is named by its `project` string,
- a **scope** is named by its `(project, scope)` pair,
- a **user** is named by its `username`,
- **system** is the literal `"system"`.

The natural key for an **agent is its `(project, scope)` pair**. A scope *is*
the channel; an agent owns exactly one scope (1-1). So callers that used to
store an `agent_id` now store `project/scope` and re-resolve (cached) when they
need to act. Internally `scopes` still uses uuids and the same SQL
`agent_id_for_scope` / `resolve_ref` shapes — it just maps to/from natural keys
at the manifest edge.

## Caching

Callers wrap the two read functions — `resolveScope` and `resolveRef` — in
`gatewayclient.RefCache` (short-TTL, positive-only). A `null` result means "not
found" and is deliberately **not** cached, so a ref that later becomes valid is
picked up on the next call. The mutating functions (`ensureProject`,
`ensureScope`) are not cached and should be called only by the flows that
legitimately own scope creation; the hot read path is validate-only
`resolveScope`.

---

## Functions

All four go in `functions[]` of the `scopes` `ready.api` manifest. Args is the
JSON object POSTed as the body of `POST /svc/scopes/fn/<fn>`; the return shape
is the JSON the gateway returns to the caller. `as_` (the `X-Awm-As` principal)
is threaded by the gateway and available to the implementation for
authorization; it is not part of the args object.

> A `null` return is rendered as `{}` over the gateway today
> (`proxy_service_http` substitutes `{}` for `None`). Where a function below is
> documented as returning `... | null`, implement "not found" as a payload the
> caller can test as falsy — return `{"exists": false, ...}` for `resolveScope`
> and `null`/`{}` for `resolveRef` — rather than relying on HTTP status. The
> shapes below spell out the not-found form for each.

### `resolveScope(project, scope) -> {exists, project, scope, status} | null`

Replaces `agent_id_for_scope` / `agent_record_for_scope`. The validate-only hot
read: "does this scope exist, and is it live?" Callers store `project/scope`
directly and call this (through `RefCache`) before acting on a reference.

- **args:** `{ "project": str, "scope": str }`
- **returns (found):**
  ```
  { "exists": true, "project": str, "scope": str, "status": str }
  ```
  where `status` is the agent's lifecycle status — one of
  `"allocated" | "active" | "retired"` (mirrors `agents.status`; the underlying
  query is the `active_only=False` form of `agent_id_for_scope`, i.e. it
  resolves retired scopes too, and the caller decides whether `retired` counts).
- **returns (not found):** `{ "exists": false, "project": str, "scope": str }`
  — the `project`/`scope` echoed back, `exists: false`, no `status`. This is the
  falsy "not found" signal; `RefCache` will not cache it.

No `agent_id` is ever returned. The natural key the caller keeps is
`(project, scope)`.

### `resolveRef(literal) -> {kind, project?, scope?, username?} | null`

Replaces `identity.resolve_ref` — messaging author/recipient resolution.
Resolves one literal author/sender/recipient string to its natural-key
identity. Literal forms accepted (verbatim from `resolve_ref` in `identity.py`):

- `"agent:<project>/<scope>"` → an agent scope
- `"scope:<project>/<scope>"` → an agent scope
- `"<project>/<scope>"` (bare, contains a `/`) → an agent scope
- `"user:<name>"` → a user
- `"system"` or `""` → the system sentinel
- a bare opaque literal (no `/`) → treated as a username

- **args:** `{ "literal": str }`
- **returns:**
  - agent: `{ "kind": "agent", "project": str, "scope": str }`
  - user:  `{ "kind": "user", "username": str }`
  - system: `{ "kind": "system" }`
  - **not found:** `null` (rendered `{}` over the gateway) — e.g. an
    `agent:.../...` whose scope does not exist, or a `user:<name>` that does not
    exist. `RefCache` will not cache it.

The boundary difference from `identity.resolve_ref`: the old function returned
an internal uuid (or the `"system"` sentinel string). This returns the
*natural key* of whatever it resolved — `(project, scope)` for an agent, the
`username` for a user, `kind: "system"` for the sentinel. Callers that need to
attribute a message store the returned natural key, not a uuid.

> User-on-demand creation: `identity.resolve_ref` had a `create_users` flag
> that inserted an unknown `user:<name>` on the fly. That side-effecting
> behavior does NOT belong on this read function — `resolveRef` is pure lookup
> and returns `null` for an unknown user. Flows that legitimately mint a user
> on first reference call a creation function (out of scope for this contract;
> add a `ensureUser` to the manifest if/when a flow needs it) rather than
> getting creation as a side effect of resolution.

### `ensureProject(project, repo_path?, url?) -> {project}`

Replaces `identity.ensure_project`. Idempotent get-or-create of a project.
Used by scope-creation flows that legitimately own that lifecycle — not the
hot read path.

- **args:** `{ "project": str, "repo_path"?: str, "url"?: str }`
  - `repo_path` is the project's on-disk repo path (the underlying
    `ensure_project` takes it as a keyword; supply it on first create).
  - `url` is set on insert and preserved on subsequent calls.
- **returns:** `{ "project": str }` — the natural key, echoed. (No project
  uuid crosses the boundary.) Idempotent: calling again for an existing project
  returns the same `{project}` without error.

### `ensureScope(project, scope, ...) -> {project, scope}`

Replaces `identity.ensure_agent`. Idempotent get-or-create of an agent/scope
under an existing project. The optional fields mirror `ensure_agent`'s keyword
arguments.

- **args:**
  ```
  { "project": str, "scope": str,
    "branch"?: str, "worktree"?: str, "agent_cli"?: str,
    "status"?: str, "is_vagrant"?: bool, "display_name"?: str }
  ```
  Defaults follow `ensure_agent`: `branch` defaults to `feat/<scope>`,
  `agent_cli` to `"claude"`, `status` to `"allocated"`, `is_vagrant` to
  `false`, `display_name` to the `scope`. Requires the project to exist
  already (call `ensureProject` first); a missing project is an error
  (`ensure_agent` raises `KeyError`, which the implementation should surface as
  a non-2xx so the caller sees a `GatewayCallError`).
- **returns:** `{ "project": str, "scope": str }` — the agent's natural key.
  No `agent_id`. Idempotent: an already-live scope for the same name returns
  the existing one.

---

## Mapping back to `identity.py`

| Contract fn      | `identity.py` source                                   | Boundary change                                  |
|------------------|--------------------------------------------------------|--------------------------------------------------|
| `resolveScope`   | `agent_id_for_scope` / `agent_record_for_scope`        | returns `(project, scope, status)`, never `agent_id` |
| `resolveRef`     | `resolve_ref`                                          | returns natural keys, never uuid; no `create_users` side effect |
| `ensureProject`  | `ensure_project`                                       | returns `{project}`, never project uuid          |
| `ensureScope`    | `ensure_agent`                                         | returns `{project, scope}`, never agent uuid     |

Reverse-direction helpers in `identity.py` (`project_scope_for_agent`,
`display_for_ref`, `username_for_user_id`) take a uuid and exist only because
the old code stored uuids. With natural keys on the boundary they have no
cross-service caller — they stay internal to `scopes` (used where `scopes`
renders its own data) and are not part of this manifest.
