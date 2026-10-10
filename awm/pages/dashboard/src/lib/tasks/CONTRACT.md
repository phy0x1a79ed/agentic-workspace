# Dashboard ↔ backend contract (future)

The dashboard board renders against `client.ts`, which today serves mock
fixtures (`USE_MOCKS = true`). This file documents the backend surface a
later round (in awm-internal / svc-* scopes, **not** web-ui) must implement so
the seam can flip to live data with zero changes to the view layer.

## Tasks — net-new (nothing exists today)

No task/work-item table, model, or endpoint exists in awm. All of this is greenfield.

| Method | Path | Returns | Notes |
|---|---|---|---|
| `GET`  | `/projects/{name}/tasks` | `{ tasks: Task[], total }` | One project's board. |
| `GET`  | `/tasks/{id}` | `Task` | Detail drawer. |
| `POST` | `/tasks/{id}/retry` | `Task` | Re-queue; spawns the outer-loop agent for the task. |
| `POST` | `/tasks/{id}/resolve` | `Task` | Human reviewed: clear `needsHuman`, mark reviewed. |
| `WS`   | `/projects/{name}/tasks/attach` | frames `{ type: 'task', task: Task }` | Live board updates. |

`Task` / `TaskStatus` / `TaskResult` shapes: see `types.ts` (the source of truth this contract must match).

### Suggested storage
A `tasks` table keyed by project, with `status`, `assigned_scope` (the
`(project, scope)` that runs it), `depends_on` (task ids), `result` JSON, and a
`needs_human` flag. The orchestrator agent (parent) writes tasks; task agents
(children) update `status`/`result`. "Blocked" is derived when any `depends_on`
task is not `completed`.

## Adjacent gaps the dashboard also binds to later

These already half-exist; the dashboard's non-board panels wait on them.

1. **`GET /config` / `PUT /config/{key}`** (Settings view).
   `config_service.get_config/set_config` exist with no HTTP route. Wrap them.
   Known keys: `agent_cli`, `vagrant_scopes_repo_url`.

2. **`status_update` metadata convention** (Overview "needs your input" feed).
   Agents already post `inbox_send(msg_type='status_update', metadata=…)`.
   Standardize a `{ blocked: bool, reason, taskId? }` metadata schema so the
   feed can render "blocked, needs human" vs "completed, awaiting review".

## How the flip happens

In `client.ts`, set `USE_MOCKS = false` and uncomment the `apiFetch` branch in
each function (using `apiFetch` from `@awm/client`). The view files import only
the client functions, never the mocks — so nothing else changes.
