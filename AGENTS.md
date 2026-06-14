# AWM Internal Architecture

*Internal architecture reference for agents working ON awm itself: Service Hub protocol, package model, Python/env conventions. Auto-injected only when the agent's cwd contains this file at its root — `projects/awm/*` scopes inherit it via .bare-worktree sharing; other projects' agents never see it. **Do not merge into WORKSPACE.md** — that file is universal, this one is awm-private; keeping them separate is what keeps non-awm agents' contexts uncluttered.*

For workspace structure (paths, MCP tools, project map, scope lifecycle) see `WORKSPACE.md` (auto-injected before this file). This file assumes you're modifying awm itself.

## Modular tree migration (feature fanout complete)

awm is being split from the monolithic `awm/` package into a nested tree of pip dists under `awm/`: the **gateway** (`awm/gateway/`, package `awm.gateway`) — the sole interface + coordination hub — plus shared components (`awm.config`, `awm.persistence`, `awm.gatewayclient`, `awm.agentcore`) and self-contained feature services (`awm.scopes`, `awm.agents`, `awm.artifacts`, `awm.skills`, `awm.discord`, plus the voice pair `awm.stt`/`awm.tts`). The **gateway boots standalone** (de-DB'd — it owns no tables); feature surfaces appear as those modules register.

Migration status: the **foundation pass + all five feature modules are migrated**. `discord` was the proven reference; `skills`/`scopes`/`agents`/`artifacts` followed it onto their own per-service DBs + DAO + seed + `hub_adapter` on `ServiceAdapter`. Verified end-to-end in an isolated workspace (`/tmp/awm_modtest_full.sh`): all 5 services register → 60 ops project into `/tools` → the cross-service identity path is live (`ensureProject`→`ensureScope`→`resolveScope`; `artifacts.register` RPC-validates `(project, scope)` against the live `scopes` service before writing). `setup.sh` is repointed at `awm/gateway/install.sh` (the composition root). **Tests: retired + runnable as one suite.** The legacy `awm/tests/` tree is gone; gateway-owned tests moved into `awm/gateway/tests/`, feature tests live in each dist's `tests/`, and `scripts/run-tests.sh` runs every dist (each with its own `PYTHONPATH` — the dist roots can't be mixed under the shared `awm` namespace). All six dists green (gateway 145 / scopes 94 / agents 37 / artifacts 16 / skills 27 / discord 8). Root `pyproject.toml` no longer points `testpaths` at `awm/tests`.

**Rooms collapsed into the scope (a scope IS the channel).** The old `rooms`/`messages`/`session_logs` tables + their `rooms.py`/`messaging.py`/`sessions.py` engines, the `/rooms` router, `rooms_names`, and the `room_*`/`inbox_*`/`session_*` op families are **deleted**. One `scope_posts` table (kind = `message`/`journal`/`system`/…) + `scope_subscribers` replaces them, addressed by `(project, scope)`; non-agent inboxes (`user:`/`project:`/`workspace`) are non-literal channels (`owner_project=''`). The surface is `scope_post` / `scope_fetch` / `scope_subscribe` / `scope_unsubscribe` (in `awm/services/scopes/awm/scopes/channel.py` + `operations/scope_channel.py`). **Subscribe to agents, message scopes:** raw agent acts stay in `agents.db` (`agent_transcript`), exposed by the agents service's new `agent_subscribe`; the agent's rendered output is posted to its scope channel via `scope_post`. `scopes`'s `seed.py` folds the legacy trio into `scope_posts` (multi-room-per-scope merges by `ts`; journal structure → `meta`; read-state + resolve dropped) — a dry-run against the live 11 MB `state.db` reconciles exactly: 1471 posts = 454 session_logs + 242 messages + 775 room_transcripts. The deferred `roomAgentsKillOnClose` reverse-notification is moot (no rooms).

**`agentcore` — the pluggable harness layer (2026-06-13).** `awm.agentcore` (`awm/service_components/agentcore/`, a leaf imported-source component — zero `awm.config`/gateway imports) owns "talk to a CLI agent harness and yield normalized events," and nothing about scopes or the gateway. `open_agent(AgentConfig{harness:'claude'|'opencode', mode:'live'|'oneshot', model?, params?, permissions='full', workdir?, resume_id?, mcp_config?, system_prompt?}) -> AgentSession` (`subscribe()`/`send()`/`close()`); `run_once(config, prompt, schema?)` is the one-shot wrapper. Both harnesses are **subprocess** drivers — claude (`--print` stream-json, `--permission-mode=bypassPermissions`) and opencode (warm `opencode serve`, `--dangerously-skip-permissions`), **no Agent SDK, no OpenRouter** — mapped explicitly into one `AgentEvent{id, kind:'message'|'partial'|'tool_use'|'tool_result'|'status'|'result'|'error', text?, data?, ts}`; `id` is the dedupe/cursor key end-to-end. The agents service builds its sessions through it, and `stt`'s convo-cleanup uses `run_once(harness='opencode', mode='oneshot')`. The chat data-flow is **asymmetric**: the frontend POSTs human messages to the scope (`scope_post`) and SUBSCRIBES to the agents service for output; the agents service subscribes to its scope's channel to feed the agent's stdin, and records the agent's per-turn acts in `agent_transcript` (exposed live over the agents `transcript` direct WS session — backfill-from-cursor + live push, de-duped by act `id`) — **not** auto-posted back to the scope. Only deliberate agent messages (e.g. this debrief) are an explicit `scope_post`. Frontend: `@awm/agent-chat` composes `@awm/stt-composer` (STT) + `@awm/tts-history` (transcript + TTS) + `@awm/client` (`spawnAgent` busy-tolerant, `subscribeAgent`/`fetchAgentTranscript`, `postToScope`); the `agent` page is a thin mount.

**Cutover DONE (2026-06-12): prod runs the modular gateway.** `feat/full-modular-services` was promoted feat→dev→release; the env was reinstalled via `awm/gateway/install.sh` from the release tree (old monolith dist + stray feat-worktree installs uninstalled first; the gateway pyproject overwrites the `awm`/`awm-mcp` console scripts); the live `state.db` was folded into per-service DBs via `python -m awm.<svc>.seed` for all five services (rooms+messages+session_logs → 1474 `scope_posts`, reconciled exactly, 0 skips); and `awm.service` was flipped to boot the gateway. `state.db` is now only a backup/seed source (live copy at `.awm/state.db.pre-modular.bak`) — the gateway uses the per-service DBs under `.awm/services/<svc>/`. Full procedure + reconciliation numbers: memory `awm-modular-cutover-procedure`.

**Respawn durability — FIXED 2026-06-12 (was silently broken).** The journaled services are now durable across `awm.service` restarts and individual service deaths (both verified: a `systemctl restart` reconcile-respawns all 5 to ready; killing one process → fresh PID back to ready). Two respawn bugs were found and fixed when prod was discovered stuck at 3 tools (services had died on the last restart): (1) `start.sh` did `exec mamba run …`, but `supervisor.spawn_service` hands the child the gateway's env, which under systemd has a minimal PATH with no miniforge → `mamba: not found`. Fix: `awm/gateway/install.sh` bakes the env's absolute interpreter into a gitignored `awm/services/<svc>/.runtime-env` (`AWM_PYTHON`/`AWM_ENV_BIN`); each `start.sh` sources it and `exec "${AWM_PYTHON:-python}" -m awm.<svc>.hub_adapter` (no `mamba run` — the direct env interpreter runs torch/sentence-transformers fine). (2) `reconcile_journaled_services` injected `AWM_HUB_URL` from `entry["hub_url"]`, absent on manual-launch journal entries → empty URL → service died with "AWM_HUB_URL not set". Fix: the supervisor injects its own `config.HOST/PORT` URL (journal field only as override). The services only *seemed* durable at cutover because they were hand-launched with `mamba` on PATH; the first real respawn exposed both.

**Lifecycle hardening — SHIPPED 2026-06-13 (no auto-reload, no orphans, full-coverage teardown).** The gateway↔service lifecycle now covers all start/stop/crash directions deterministically; the editing-leaks-orphans failure is gone. Pieces (all in shared code → apply to **prod**, not just the dev sandbox, except T1):
- **No dev auto-reload (T1, dev-only).** `awm/gateway/dev/run.sh` launches uvicorn **without** `--reload`/`--reload-dir`. Backend edits require an explicit `awm dev restart`; saving a file no longer swaps the worker out from under its spawned services (which was the primary orphan leak). The old "editing under `--reload` leaks orphans" gotcha no longer applies.
- **Service-side give-up (T2, `gatewayclient/adapter.py`).** `ServiceAdapter.run()` exits 0 (no retry) on a stand-down signal — a `409` register, a control-WS close `4409` (lease held) / `4404` (unknown), an upgrade-`409`, or an in-band `{"kind":"shutdown"}` frame — via a `GiveUp` sentinel. It also exits once the gateway has been unreachable past `AWM_RECONNECT_DEADLINE_S` (default 10s, env-tunable), measured from the last confirmed-up disconnect (a transient blip still retries with backoff). This is the backstop for a hard-killed gateway, where lifespan shutdown never runs.
- **Duplicate rejection (T3, `api/hub.py::service_register`).** A second instance registering under a name whose record still holds a **live** control-WS lease gets `409` and stands down; the incumbent is untouched. Takeover of a **dead** (lease-not-held) record still replaces in place. Respawn-by-sid skips `_register` entirely, so it never trips this.
- **Graceful teardown (T4, `server.py`).** On SIGTERM/SIGINT the gateway drains its services in-band, then force-kills any straggler, then **clears the journal** so the next boot bootstraps clean. The drain logic is one reusable `server._drain_services()` coroutine. **In-band delivery — FIXED 2026-06-13 (was a force-kill-only path):** a **loop-level signal override** installed at lifespan startup takes ownership of SIGTERM/SIGINT *before* uvicorn's own handler runs, so the gateway delivers the `{"kind":"shutdown"}` frame over each live control WS **while it is still open** — each service stands itself down (`adapter.py` "shutdown frame received; standing down" → "giving up"), drops its lease, and exits. Force-kill is now the **backstop, not the primary mechanism**: it fires only for a straggler whose lease is still held after the ~8s grace window, OR whose process is still alive while its lease is already gone (the fallback path below). The override captures uvicorn's `Server` off `signal.getsignal(sig).__self__` — **this uvicorn (0.x) registers `signal.signal(sig, server.handle_exit)`, NOT `loop.add_signal_handler`** (it says so in `capture_signals`), so the original plan's `loop._signal_handlers` capture found nothing; getsignal is the correct seam — then installs `loop.add_signal_handler(sig, _on_signal)` (works on uvloop too) which drains then flips `server.should_exit`. A **second** signal force-exits (operator escape hatch). If the Server can't be captured (TestClient / non-main-thread / future internals change) it **falls back** to the flag-only wrapper and the drain runs from the lifespan-shutdown backstop (the pid-alive force-kill path). The idle self-stop (`_idle_shutdown_loop`) and lifespan shutdown both call `_drain_services()` too (idempotent). dev caveat: `dev/run.sh` records the **mamba-run wrapper** PID in `dev.pid`, so a bare `kill -TERM $(cat dev.pid)` hits the wrapper, not uvicorn — `awm dev stop` (which `pkill -P`s the uvicorn child) and prod `systemctl stop` (systemd signals the `awm gateway serve` process directly) both deliver the clean signal that triggers the in-band path.
- **Crash-respawn watchdog (T5, `supervisor.supervise_disconnect` + `hub.py` disconnect hook).** An unexpected control-WS disconnect schedules a watchdog: re-register the record (so a quick self-reconnect is accepted), wait `_RECONNECT_WINDOW_S` (10s), and respawn from the journal if still silent. Gated on not-shutting-down + journal-entry-present + enabled + not-reconnected. `awm services stop` now drops the journal entry **before** killing, so a deliberate stop is never respawned. Two independent 10s windows now exist — the service's give-up deadline (T2) and the gateway's reconnect/respawn window (T5) — complementary (service-side vs gateway-side authority).

**Feature-service first-boot bootstrap — SHIPPED (reconcile-then-bootstrap).** Gateway boot no longer depends on a populated journal. On boot it (1) `reconcile_journaled_services()` for anything already journaled, then (2) **bootstraps** any discovered-but-unjournaled service that is enabled — so a fresh clone or a wiped `.awm/state/services.json` comes up with every enabled service running, no manual kick. Discovery is a filesystem scan of `awm/services/*` for `run.sh` (`awm/gateway/awm/gateway/hub/discovery.py`) — no hardcoded name list, no manifest-sync step (the old `packages sync` CLI is gone). Per-service **enable/disable** state lives in `.awm/services/enabled.json` (a service absent from the file is enabled); a disabled service stays down across restarts. The operator surface is `awm services list|start|stop|restart|enable|disable [name|--all]` (`start --all` starts every enabled service). Key foundation facts for anyone touching this:

- **Per-service SQLite DBs, no shared `state.db`.** `awm.persistence.databases` is the factory — `get_connection(service)` + `init_service_db(service, schema_sql, schema_version=…)` give each service its own DB at `AWM_DIR/services/<svc>/<svc>.db`. Raw SQL lives behind `awm.persistence.dao.BaseDAO` subclasses shipped per-service. The old `db.py`/`migration_v37.py` are deleted; the legacy `state.db` survives on disk only as a read-only seed source (each service self-seeds via its own `seed.py`). Per-service schema + legacy seed shapes are documented in `awm/service_components/persistence/SCHEMA_HANDOFF.md`.
- **No global identity.** Cross-service refs are natural keys (an agent = its `(project, scope)` pair) validated by calling the owning service over gateway RPC (cached), never by importing it. `scopes` owns identity and exposes it via the RPCs frozen in `awm/services/scopes/IDENTITY_CONTRACT.md`.
- **Services are OUT-OF-PROC**, spawned + PID-journaled by the supervisor; the control-WS lease is liveness (10s reconnect+respawn). Each service is a self-contained, executable `run.sh` (the only thing the gateway runs — `bash run.sh`) plus a tiny `hub_adapter.py` built on **`awm.gatewayclient.ServiceAdapter`** (the reusable register→ready→serve→dispatch→reconnect base). The gateway injects **exactly three** env vars and no more: `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_SERVICE_ID` — there is NO auth, no token, ever. `awm.gatewayclient` also carries `call`/`call_sync`/`RefCache` for the service→service direction. The `/svc/*` control plane is unauthenticated (loopback only). `awm/services/discord/` is the worked example to copy; the human-facing authoring contract is README § *Authoring a service*.

- **Install/iterate each module via its own `install.sh`, never hand-rolled `pip`.** `awm/gateway/install.sh` is the composition root — it installs `config` + `persistence` and every feature dist `--no-deps`, then the gateway itself (resolving third-party deps). Components (`config`, `persistence`, `gatewayclient`, `agentcore`, `ui_components`) are pure imported source and have no `install.sh`. Override the target env with `AWM_ENV`.
- **Registration contract.** How a service declares its API (the serializable `ready.api` manifest — `functions`/`emitters`/`subscriptions`), how that threads into the MCP/CLI/HTTP generation layer (manifest → `Operation` → the unchanged `operations.py` compiler; dispatch is catalog-owned), and the **hub-mediated** service↔service comms model (`call` request/reply, `emit`/`sub` pub/sub, `Bridge` streaming — all identity-aware via `as_`, never direct sockets) are documented in `awm/gateway/awm/gateway/catalog.py`. The catalog is the single live source the gateway renders to all three surfaces; `/tools` and `/invoke` read from it. A manifest function may carry an optional `"tool"` key to set its exact MCP-surface name, decoupling the projected label from the internal op `name` used for RPC dispatch — this is how the surface reads as clean `<domain>_<verb>` (`scope_create`, `agent_spawn`, `scope_resolve`) while internal names (incl. the frozen `IDENTITY_CONTRACT` camelCase RPCs) are unchanged. `_tool_name` honors it; `list_tools` warn-and-skips duplicate projected names (override names must be globally unique). The gateway's **own** control plane now runs through the same compiler: its status/restart/mcp-sync, hub list/deregister, and services-lifecycle ops are declared once as `GATEWAY_OPERATIONS` (a list of `operations.Operation` in `awm/gateway/awm/gateway/gateway_ops.py`) and the three generators in `operations.py` (`operations_to_mcp_tools`, `register_fastapi_routes`, `register_cli_commands`) project them onto MCP (via `catalog.list_tools`/`dispatch`), HTTP (`server.py` startup), and CLI (`cli.py`) — so a new gateway control op is added as one `Operation`, never hand-rolled as a CLI command + HTTP route + native MCP tool in three places.
- **Concurrency.** One server loop owns all async hub state; blocking work is offloaded (`run_in_threadpool` / `run_in_executor`); `asyncio.run()` is banned inside the daemon (survives only in the CLI/MCP-proxy/entrypoint processes). `catalog.dispatch` is async, `/tools` is sync.

## Federation: retired

Federation is gone — see git history for the deletion (commits S1–S5, S6, S8 on `dev` around 2026-06-04). No `/peer/*` routes, no `awm peer *` CLI, no `awm-exposed.service`, no auth layer, no `peers`/`peer_sync_state` tables. The local listener (`awm.service` on `127.0.0.1:7819`) is the only listener. Single host, no auth, plain HTTP loopback.

## Service Hub

`awm.gateway.server:app` is the gateway — the sole interface + coordination hub. **Rooms collapsed into the scope** (a scope IS the channel): there is no `/rooms` router and no `rooms`/`messages`/`session_logs` tables; the scope-channel surface (`scope_post`/`scope_fetch`/`scope_subscribe`) is served by the `scopes` feature service over `/svc/scopes`. The gateway owns no tables of its own. A few path prefixes are *registered* at runtime; matched requests dispatch to one of four kinds:

| Kind | Surface | Typical caller |
|---|---|---|
| `service` | RPC-over-WS at `/svc/<name>` | a folder under `awm/services/<name>/` discovered + run by the gateway |
| `page` | static bundle at `/ui/<name>` | a built bundle under `awm/pages/*` |
| `url` | HTTP/WS proxy at any prefix | external services registered via `awm gateway register --url ...` |
| `static` | static bundle at any prefix | external bundles registered via `awm gateway register --dir ...` |

Each prefix maps to a stack of records (base + optional overlays). Overlays are pushed by `awm dev shadow`; the topmost overlay receives traffic, with base traffic resuming instantly when an overlay's lease closes.

### Feature services — the `awm/services/<name>/` contract

The first-class way to add a backend is a folder under `awm/services/<name>/` with a self-contained executable `run.sh`. The gateway **discovers** it by filesystem scan (`awm/gateway/awm/gateway/hub/discovery.py`), **bootstraps** it on first boot, respawns it durably, and injects **exactly three** env vars (`AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_SERVICE_ID`) — no auth, ever. Enable/disable state lives in `.awm/services/enabled.json`; the operator surface is `awm services list|start|stop|restart|enable|disable [name|--all]`. This **supersedes** the old `kind=stripe` / `packages/services/*` stripe-authoring flow (the `svc-*` + `comp-*`-registered-via-`awm gateway register` vertical-stripe model). The full human-facing authoring contract is **README § *Authoring a service***.

The `url` / `static` kinds and the `awm gateway register --url|--dir` lease commands remain only for **external** bundles/services (a remote HTTP/WS upstream, or a hand-built static dir) — not for awm's own backends, which are `awm/services/*` folders.

### Hub origin = gateway port

There's only ever one hub origin per node — the gateway (`awm.gateway.server:app`) process. Which port that is depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd-managed) | `7819` | `awm.service` on the host |
| Dev sandbox: `projects/awm/dev/` | `7821` | `awm dev start` |
| Dev sandbox: `projects/awm/web-ui/` | `7831` | same |
| Dev sandbox: `projects/awm/web-backend/` | `7841` | same |
| Dev sandbox: any other scope (fallback) | `7851` | same |

Per-scope port bands are derived from the worktree dirname, so dev sandboxes run side-by-side with prod (and each other) on distinct ports.

**Substitute your sandbox port** whenever this section says `:7819`. The CLI's own target hub is `BASE_URL`, computed from `AWM_PORT` (default `7819` = prod) — `AWM_HUB_URL` is injected into *services*, not consulted by the CLI. `awm dev shadow` takes a `--port` (default `7821`, the dev sandbox) to pick which hub it shadows onto, so you don't have to set `AWM_PORT` (and can't fat-finger a shadow onto prod).

### One-time per node

```bash
awm dev start                   # bring up the dev hub (or skip if prod awm.service is already up)
export AWM_WORKSPACE=$PWD       # so the awm CLI uses this sandbox's .awm/
```

No auth bootstrap: the listener is loopback-only, no bearer or cookie is required.

### When to make a `svc-*` scope

A `svc-*` scope is for cross-cutting work on a single backend service — the service itself lives in `awm/services/<name>/` regardless of which scope is editing it. Similarly, a `comp-*` scope is for cross-cutting work on a single shared component. Day-to-day, README § *Authoring a service* / § *Authoring a page* are the authoritative workflow docs.

### External registrations (`kind=url` / `kind=static`)

For an **external** upstream the gateway should front, register a lease via `awm gateway register`. These POST/DELETE/WS to `/hub/*` on the gateway origin; useful as the raw API when scripting:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/hub/register` | Register a service; returns `service_id` + `lease_ws_path` |
| `WS`     | `/hub/lease/{service_id}` | Hold lease; disconnect → eviction |
| `GET`    | `/hub/services` | List registrations + lease state |
| `DELETE` | `/hub/services/{name}` | Force-evict by name |

All are unauthenticated — the gateway binds loopback only and has no auth layer. `kind=static` serves canonical paths only (a file at the exact path, or a directory's `index.html`; a miss is a 404 — no `Accept` fallback, no SPA shell synthesis). For deep-link refresh in a SvelteKit/React-Router bundle, prerender every route; for routes the server can't enumerate, front the upstream as `kind=url`.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix. `/hub` and `/hub/*` are reserved.
- **`AWM_WORKSPACE` + `AWM_HUB_URL` attach to a sandbox, not prod.** Without them, the CLI hits global discovery and may target prod `:7819`. The dev starter exports both for its children; if you shell out separately, export them yourself. To check which hub a running process was set up against: `tr '\0' '\n' </proc/<pid>/environ | grep -E 'AWM_WORKSPACE|AWM_HUB_URL'`.
- **Never run two gateways on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are explicitly supported and how dev parallelism works.

## Authoring a service / page — see `README.md`

The day-to-day "how do I ship a service / page / component" workflow lives in
[`README.md`](README.md) § *Authoring a service* and § *Authoring a page*. Those
docs cover the `awm/services/<name>/` folder contract (the self-contained
`run.sh`, `INSTALL.md` / `install.sh` + `.runtime-env`, the three injected env
vars, discovery/bootstrap/enable-disable, `awm services`, `awm dev shadow`), the
`npm install → build` page pipeline (the generated `package.json` is checked in
— there is no longer a `packages gen` CLI step), and the hub-service
control-plane envelopes.

This file (AGENTS.md) is for **internal** awm work — modifying the gateway, the
registry, the supervisor, the RPC envelope layer, the operations/catalog
generation layer, etc. Where README.md tells you how to use the system, the rest of this file
tells you where the implementation lives (modular tree under `awm/gateway/awm/gateway/`):

- **Service discovery** — `awm/gateway/awm/gateway/hub/discovery.py` (filesystem scan of `awm/services/*` for `run.sh`; reads `.awm/services/enabled.json`).
- **Registry overlay + kinds** — `awm/gateway/awm/gateway/hub/registry.py` (one `_stacks` dict per prefix; base + overlays LIFO; `kind` Literal = `url` | `static` | `page` | `service`).
- **RPC layer** — `awm/gateway/awm/gateway/hub/rpc.py` (in-memory `ControlChannel` per service, `_pending` call table, subscriber registry, session table, bridge id allocator).
- **Service translator + bridge** — `awm/gateway/awm/gateway/hub/proxy.py::proxy_service_http` / `open_session_via_http` / `proxy_session_ws` / `proxy_service_emit_ws`.
- **Supervisor + PID journal + bootstrap** — `awm/gateway/awm/gateway/hub/supervisor.py::reconcile_journaled_services` / `bootstrap` / `spawn_service` / `kill_pid_group`; state at `<AWM_DIR>/state/services.json`. On boot: reconcile journaled, then bootstrap discovered-but-unjournaled enabled services; injects only `AWM_HUB_URL` / `AWM_SERVICE_NAME` / `AWM_SERVICE_ID` and runs `bash run.sh`.
- **Catalog (manifest → MCP/CLI/HTTP)** — `awm/gateway/awm/gateway/catalog.py` (`_tool_name`, `dispatch`, `/tools`/`/invoke`).
- **CLI** — the `gateway` + `services` command groups are **generated** from `GATEWAY_OPERATIONS` (`awm/gateway/awm/gateway/gateway_ops.py`) by `register_cli_commands` (`operations.py`), with a few hand-authored commands attached; `awm dev shadow` (search for `dev_app`) lives alongside them in `awm/gateway/awm/gateway/cli.py`. The old `packages` CLI group (and `packages_app`) was removed in the control-plane consolidation.

The Service Hub section above carries the *external* contract (the `awm/services/<name>/` folder contract, which kinds exist, the port table); this section tells you which files implement each piece if you're about to change them.

> The `awm/gateway/awm/gateway/` nesting is **intentional** — a PEP 420 namespace layout so multiple uninstalled worktrees can each shadow `awm.gateway` via `PYTHONPATH` without colliding. It is not a bug or a stutter to "fix."

## Awm Editable Install Gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `/home/tony/lib/miniforge3/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds:

- Run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping, OR
- Spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree, OR
- Merge dev → release to advance the editable mapping's target (what the deploy step does).

See memory `[[awm_two_source_trees]]` for the full failure mode.

## Running tests

There is no single repo-wide `awm/tests/` tree any more, and no root `pyproject.toml`. Tests are **per-dist**: each dist (gateway + the five feature services) owns its own `tests/` directory and its own `pyproject.toml` (which carries that dist's pytest config — `asyncio_mode`, markers). The dists all merge into the PEP 420 `awm` namespace, so a single `pytest` over the whole tree can't import every dist at once (two source roots both providing `awm` shadow each other). The runner invokes pytest **once per dist** with that dist's source root + the shared components (`config`/`persistence`/`gatewayclient`) on `PYTHONPATH`:

```bash
# Full suite — every dist, each with the correct PYTHONPATH. Run before merging.
awm/gateway/scripts/run-tests.sh

# Only the named dists:
awm/gateway/scripts/run-tests.sh scopes gateway

# Pass extra args through to pytest (e.g. fail-fast, quiet):
PYTEST_ARGS="-x -q" awm/gateway/scripts/run-tests.sh
```

The script reports pass/fail per dist and exits non-zero if any dist failed. Known dists: `gateway scopes agents artifacts skills discord`. Cross-dist imports inside a test must be **lazy** (inside a fixture/function), never at module top level — a top-level cross-dist import re-triggers the namespace-shadowing problem the per-dist runner exists to avoid.

## Agent Rules

1. **Keep WORKSPACE.md and AGENTS.md audience-pure** — workspace-structural goes in `WORKSPACE.md`, awm-internal goes here. If you find yourself adding path tables or MCP catalogs to this file, they belong in `WORKSPACE.md` instead.
2. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it's the mechanism that keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
3. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.
