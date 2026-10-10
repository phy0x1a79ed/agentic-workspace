# AWM Architecture

## Purpose & Contents

This file is the reference for agents that change awm itself. It covers the gateway, the feature-service contract and lifecycle, the catalog that renders services onto MCP, HTTP and the CLI, the tier split and the verb gates, dev sandboxes, the data-layer internals, the frontend component system, federation pitfalls, a file map, and how to run tests.

Agent orientation goes in `AGENTS.md`. Workspace procedures go in `PROTOCOLS.md`. Human install and service authoring go in `README.md` § *Authoring a service*. Cross-node design goes in `FEDERATION.md`.

Keep what the code cannot show: why a shape was chosen, which two things must agree, and what breaks silently when they stop agreeing. Point at the source for everything else.

## Overview

awm is a modular **gateway** plus a set of out-of-process **feature services**. The gateway (`awm/gateway/`, package `awm.gateway`) is the only interface (CLI, HTTP, MCP stdio) and the coordination hub. It owns no tables and boots standalone. Feature surfaces appear as their services register.

The tree is a set of pip dists under `awm/` that merge into the PEP 420 `awm` namespace:

- **Gateway** — `awm.gateway`. It discovers, bootstraps and supervises services, and renders their APIs onto MCP, HTTP and the CLI.
- **Shared Python components** — `awm/service_components/`: `awm.config`, `awm.persistence`, `awm.gatewayclient`, `awm.claudedaemon`. These are imported source with no `install.sh`.
- **Frontend source** — `awm/ui_components/` and `awm/pages/`. See § *Frontend*.
- **Feature services** — `awm/services/<name>/`, each with its own dist, DB and `run.sh`. `awm services list` reports the live set. Each folder's `INSTALL.md` is its contract.

Two facts about services are not visible in the directory:

- `skills` is retired. The rest of its catalog is reference-only on disk.
- `compute` acts on processes it does not own. It kills and renices agent-launched jobs. Add anything new that agents launch and must keep (a long-lived server, a tunnel, an MCP shim) to its `PROTECTED` list.

The `awm/gateway/awm/gateway/` nesting is intentional. It is a PEP 420 layout that lets several uninstalled worktrees each shadow `awm.gateway` through `PYTHONPATH`. Do not flatten it.

### Ownership and identity

Each feature service owns its own SQLite DB. There is no shared `state.db`. Services refer to each other's records by natural key, such as an agent's `(project, scope)` pair. A service validates such a key by calling the owner over gateway RPC, never by importing it. `scopes` owns identity and exposes it through the RPCs frozen in `awm/services/scopes/IDENTITY_CONTRACT.md`.

### claudedaemon

`awm.claudedaemon` is everything awm knows about Claude Code's background sessions. It reads the daemon roster and the per-job and per-process records (`roster`), launches a session (`launch`), checks directory trust (`trust`), types a line into a session over the daemon's PTY socket (`pty`, `lane`, `job`) and answers which restricted mode a session runs in (`sessionmode`). It decides nothing about whose session a caller may reach. `cx`, `reflection`, `transcripts`, the front door and the gateway each import it and each make that decision for themselves.

**CAUTION** `sessionmode` is the one reader of `cx start`'s lineage records. The gateway gate and `cx` both call it, so the writer and the reader resolve the same directory from the same environment variables. A second copy of that resolution reads no record for any session and answers "not restricted".

### A scope is the channel

There are no `rooms`, `messages` or `session_logs` tables. One `scope_posts` table (kind `message`, `journal`, `system`, …) and `scope_subscribers`, addressed by `(project, scope)`, carry everything. A non-agent inbox (`user:`, `project:`, `workspace`) is a non-literal channel with `owner_project=''`. The surface is `scope_post`, `scope_fetch`, `scope_subscribe`, `scope_unsubscribe` and `scope_archive_search`, in `awm/services/scopes/awm/scopes/channel.py`, `archive.py` and `operations/scope_channel.py`.

A post is passive mail. It starts no session and wakes no one. Agents on one node message each other with Claude's `SendMessage`, and swarms message each other through the board (`skills/awm/board-card.md`). Only a deliberate post, such as a debrief, enters the channel.

A post that arrives through the edge takes its author from the edge's `X-Awm-As` stamp, never from the `author` argument. The claimed author moves to `meta.claimed_author`. A caller with no stamp may not send an author of the form `peer:…`.

## Service hub

`awm.gateway.server:app` is the gateway. Path prefixes are registered at runtime. A matched request dispatches to one of four kinds:

| Kind | Surface | Typical caller |
|---|---|---|
| `service` | RPC over WS at `/svc/<name>` | a folder under `awm/services/<name>/` |
| `page` | static bundle at `/ui/<name>` | a built bundle under `awm/pages/<name>/dist/` |
| `url` | HTTP/WS proxy at any prefix | an external upstream, `awm gateway register --url` |
| `static` | static bundle at any prefix | an external bundle, `awm gateway register --dir` |

Each prefix maps to a base record and at most one live overlay. `awm dev shadow` pushes overlays. The last connect wins: a new overlay evicts the incumbent overlay on the prefix. It never stacks, and a duplicate overlay name never returns 409. The evicted shadow closes with WS code `4410` and an `evicted by <who>: <why>` notice. The base is never evicted. Its traffic resumes when the overlay's lease closes.

### The feature-service contract

A backend is a folder under `awm/services/<name>/` with a self-contained executable `run.sh`. The gateway finds it by filesystem scan, bootstraps it on first boot and respawns it. It injects exactly three env vars, `AWM_HUB_URL`, `AWM_SERVICE_NAME` and `AWM_SERVICE_ID`, and no auth.

`.awm/services/enabled.json` holds enable state. A service absent from it is enabled. The same file gates pages: `awm/pages/<name>` follows its own entry, else its same-named service. Disabling a service therefore also takes down its `/ui/<name>` page. The operator surface is `awm services list|start|stop|restart|enable|disable|reap [name|--all]`.

- **Per-service DBs.** `awm.persistence.databases` is the factory. `get_connection(service)` and `init_service_db(service, schema_sql, schema_version=…)` give each service a DB at `AWM_DIR/services/<svc>/<svc>.db`. Raw SQL lives behind per-service `awm.persistence.dao.BaseDAO` subclasses. Each service seeds itself through its own `seed.py`. `awm/service_components/persistence/SCHEMA_HANDOFF.md` documents the schemas.
- **Out-of-process.** The supervisor spawns each service and records its PID in a journal. The control-WS lease is liveness. Each service is a `run.sh` plus a small `hub_adapter.py` built on `awm.gatewayclient.ServiceAdapter`, which handles register, ready, serve, dispatch and reconnect. `awm.gatewayclient` also has `call`, `call_sync` and `RefCache` for service-to-service calls. The `/svc/*` control plane is unauthenticated and loopback-only. `awm/services/dev/` is the smallest complete example to copy.
- **Install.** Install each module through its own `install.sh`, never with hand-rolled `pip`. `awm/gateway/install.sh` is the composition root. It installs `config`, `persistence` and every feature dist with `--no-deps`, then the gateway with its third-party deps. Set `AWM_ENV` to change the target env.

### Registration and the catalog

A service declares its API as a serializable `ready.api` manifest with `functions`, `emitters` and `subscriptions`. The catalog (`catalog.py`) compiles the manifest into `Operation`s and renders them onto MCP, HTTP and the CLI. `/tools` and `/invoke` read from it. The hub-mediated comms model (`call` request and reply, `emit` and `sub` pub/sub, `Bridge` streaming, identity through `as_`, never direct sockets) is documented in `catalog.py`.

A manifest function may carry these keys:

- `"tool"` sets its exact MCP name and decouples it from the internal op `name` used for RPC. The surface then reads `scope_create` and `cx_start` while internal names, including the frozen camelCase identity RPCs, stay unchanged. Override names must be globally unique. `list_tools` warns and skips a duplicate.
- `"surfaces": ["cli","http"]` keeps a verb off MCP. The default is all three. `_domain_catalog` skips non-`mcp` functions and `_dispatch_domain` rejects them, while the CLI's flat `/invoke` still reaches them. This is how `writing` and `2fa` ship CLI-only write verbs.
- `"timeout"` sets the verb's budget. See § *The timeout ladder*.
- `"effect"` (`read`, `queue`, `write` or `secret`) and `"category"` feed the gates in § *Verb gating*. An omitted effect means `write`.

A manifest may also carry a top-level `"description"`. `_domain_blurbs` puts it before the generated domain description and adds it to the `describe` reply. It is the only guidance an agent reads before it chooses a verb. `2fa` uses it to send callers to `ssh(verb=connect)`. It applies to this node only, because a peer's blurb lives in the peer's catalog.

### Expanded and collapsed projections

`catalog.list_tools()` is the expanded surface, one tool per verb. The CLI generator (`register_service_cli_commands`) and flat `/invoke` dispatch depend on it.

`catalog.list_domain_tools()` folds the same surface into one `{verb, args}` tool per domain. It splits the projected name on the first underscore: `scope_create` becomes domain `scope`, verb `create`. Native ops fold by `cli_group` and `cli_command`. `GET /tools?view=domains` returns this view, and the MCP stdio proxy requests it. Every MCP client therefore carries the small surface, while the default `/tools`, the CLI and HTTP stay expanded.

Adding `&peers=1` widens the collapse to the fleet, and the envelope's `peer` key picks the node (`FEDERATION.md` § *Cross-peer calls*). It is opt-in because a peer fetches the plain view from this node's edge. That view must stay local-only, or the fleet advertises transitive peers that nobody can dial.

Adding `&tiers=1` narrows the fleet view to the core domains, `providersOf` and the call-through tool `more`. Both MCP proxies request `?view=domains&peers=1&tiers=1`.

`dispatch()` checks for a domain before the flat branches. When `name` is a known domain and `args` has `verb`, `_dispatch_domain` routes it:

- `verb="describe"` is answered from the catalog with no service round trip. `describe` is reserved on every domain.
- A native verb runs its `Operation`.
- A service verb resolves back to its internal function through `_find_service_fn(f"{domain}_{verb}")`, so a `name` that differs from its `tool` still routes. It then goes out over RPC with `as_` attached.

The collapse is additive. Reverting the proxy's `?view=domains` request undoes it.

**CAUTION** A domain's verbs must be unique. Folding warns and keeps the first. Keep a new `"tool"` override in real `<domain>_<verb>` form. A bare single-token override becomes its own one-verb domain. A two-word service name splits at its first underscore: `claude-science` projecting `claude_science_status` lands as domain `claude`, verb `science_status`. Such a service must pick a one-token domain, for example `science_status`.

### Tiers

A service folder's `service.toml` may set `tier = "core"`. Any other service is discoverable. Gateway-native `services` and `peer` are core by name (`catalog._NATIVE_CORE_DOMAINS`). A domain that only a peer provides is always discoverable. The tier decides only what the MCP list advertises. It never decides whether a service runs, and `profiles` never decides the tier.

A call `more(domain, verb, args, peer)` reaches a discoverable domain. Each MCP proxy rewrites it to the ordinary call for the real domain (`mcp_more.rewrite_call`) before it leaves the proxy. The gateway therefore stamps the caller and applies every gate against the true domain and verb. A `more` call that reaches the gateway with a domain skipped that rewrite and is refused. Keep the rewrite in `mcp_more.py`, the one place both proxies share.

**CAUTION** `service.toml` is also the profile gate. `discovery._read_profiles` must treat a file with no `profiles` key as baseline, enabled everywhere. Reading the absence as "gated off" would switch off every service that carries only a `tier`.

### The timeout ladder

An unanswered request is not an unreachable daemon. Four numbers answer four different questions. Collapsing two of them made a successful `scope create` report `awm daemon unreachable after 10.0s: timed out` for months.

1. A manifest function's `"timeout"` is how long the verb may legitimately run. The gateway enforces it server-side on both the `/invoke` and `/svc/<name>/fn/<fn>` paths, through `_rpc_call` and `_fn_timeout`.
2. A client's read ceiling is a backstop, never a budget. `mcp_http.DEFAULT_READ_TIMEOUT` (env `AWM_MCP_READ_TIMEOUT`) serves both MCP proxies and the generated CLI. It sits above the largest declared budget (3600s, the dvc `wait` verb), so it never fires first.
3. `GET /tools` has its own short ceiling. It blocks session startup, and the core answers it in about 19ms.
4. The reconnect window (10s) asks whether the daemon is coming back. It applies only before the request lands.

Retry the connect phase. Never retry the response phase. urllib wraps connect and send failures in `URLError`, but raises a bare `TimeoutError` in the response phase, which a catch-all swallows as an `OSError`. `/invoke` is not idempotent, and by the response phase the core is already running the call. Nothing tells it the client left: `/invoke` has no disconnect awareness, and `ControlChannel.call` enqueues onto the control WS before it awaits. A response-phase timeout therefore raises `mcp_http.CoreNoReply`, whose envelope says the request was delivered and tells the caller to check with a read verb.

Both proxies share `mcp_http` so they cannot drift. The `AWM_MCP_SDK=1` rollback once omitted `httpx.ReadTimeout` from its retry tuple and reported the same failure as `{"error": ""}`.

**CAUTION** A change here reaches agents only after their MCP clients restart.

### Verb gating

Three gates decide who may call a verb. They share one declaration. Each manifest verb states an `effect`, and a read verb may add a `category` (`journals`, `kb`). A verb with no effect counts as `write`, so a new verb stays closed until its author opens it. A native gateway op never carries a category.

1. **Foreign gate.** `catalog.dispatch` sends a caller stamped `peer:<node>` to `_dispatch_foreign` unless the peer book calls the node domestic. The caller runs a verb only if the verb is `read` and its category is in the peer record's `grants`. Every other case raises the error an unknown tool raises, which the edge returns as 404. This covers `providersOf`, any `peer` argument (no onward hop through this node) and a service's own `PermissionError` (`_refusal_as_unknown`). A refusal then reveals nothing about what exists. `/tools` and `describe` show the same cut catalog. The gate reads the peer book on every call.
2. **Edge path list.** `httpsfront/policy.py` `FOREIGN_PATHS` lets a foreign node reach `/tools` and `/invoke` and nothing else. The edge does not repeat the verb check. Add a path to that set only when the foreign gate covers it. `hub/proxy.py` refuses a foreign caller on every `/svc` path as a backstop.
3. **Mode gate.** A session that `cx start` launched in a restricted mode may call only the verbs `awm.config.modes` lists for that mode. A policy is either an `Allow` list of exact verbs or a `ByEffect` rule. The gate runs in `gateway/server.py` on `/invoke` and on `/svc/<svc>/fn/<fn>`, and it closes every other `/svc` path to the session. A restricted session may not name a `peer`. A request that carries `X-Awm-As` came through the edge, so the mode gate skips it.

The mode gate finds the caller through its pid: `X-Awm-Session-Pid` (set by `awm-mcp`, a stdio child of the session) or `X-Awm-Caller-Pid` (a hook). `claudedaemon.sessionmode.mode_of` reads the mode from disk, and the gateway caches the answer for 5 seconds. The answer is a mode string, `None` (positively not cx-started) or `"unknown"`. Treat `unknown` as the most restricted mode, which may compact the session and nothing else. Any failure to read a record is `unknown`, never `None`.

**CAUTION** The mode gate is a guardrail on the agent-facing doors, not a trust boundary. The loopback gateway is open, and a request with no pid header carries no mode. The gate holds only while a restricted session cannot make a raw HTTP request. Its launch tool list holds no Bash or WebFetch, and no policy admits the `rlm` browser.

**CAUTION** The gate cannot cover Claude's built-in tools. The representative keeps `SendMessage` and `ListAgents`, and both reach every session on the shared Claude daemon. Its instructions limit `SendMessage` to hand-offs. One front door per swarm is a deployment rule, and nothing in code enforces it.

### The gateway's own control plane

The gateway declares its status, restart and mcp-sync ops, hub list and deregister, and service lifecycle once, as `GATEWAY_OPERATIONS` in `gateway_ops.py`. Three generators in `operations.py` project them: `operations_to_mcp_tools` onto MCP, `register_fastapi_routes` onto HTTP and `register_cli_commands` onto the CLI. Add a new control op as one `Operation`. Never hand-write a CLI command, an HTTP route and an MCP tool separately.

### Concurrency

One server loop owns all async hub state. Offload blocking work with `run_in_threadpool` or `run_in_executor`. `asyncio.run()` is banned in the daemon and survives only in the CLI, MCP proxy and entrypoint processes. `catalog.dispatch` is async. `/tools` is sync.

## Service lifecycle

The gateway and its services handle every start, stop and crash direction deterministically. Editing a backend never leaks orphans.

### Boot

At boot the gateway reconciles, then bootstraps:

1. `reconcile_journaled_services()` handles everything already in the journal.
2. The gateway bootstraps every discovered, enabled service that is not in the journal.

A fresh clone or a wiped `.awm/state/services.json` therefore comes up with every enabled service running. Discovery is a filesystem scan of `awm/services/*` for `run.sh`. A disabled service stays down across restarts.

`awm/gateway/dev/run.sh` launches uvicorn without `--reload`, so saving a backend file never swaps the worker out from under its services. Run `awm dev restart` after a backend edit.

### The ready-ASAP contract

A service registers and sends its `ready` frame before `on_start` runs, then finishes initialising in the background. `call`, `notify` and `session.open` envelopes that arrive during initialisation wait on an event, bounded by `AWM_INIT_WAIT_S` (default 60s). A caller sees a slow first call instead of an error. `on_start` is where `init_service_db` runs, so the gate also keeps handlers off a half-built database. A hung init returns a "still initialising" error. A failed init propagates out of `run()` and the process exits.

**CAUTION** Do not await slow work before `run()` or inside `on_start`. The reaper treats a long unready state as broken, so unready must mean broken, not loading. Put long startup work in a task that `on_start` spawns.

### Service-side give-up

`ServiceAdapter.run()` in `gatewayclient/adapter.py` exits 0 without retry on a stand-down signal, through a `GiveUp` sentinel:

- a `409` on register
- a control-WS close `4409` (lease held) or `4404` (unknown)
- an upgrade `409` or `403`. The gateway closes an unknown `service_id` with `close(4404)` before `accept()`, and the `websockets` client reports that as HTTP 403.
- an in-band `{"kind":"shutdown"}` frame
- a close `4410` (evicted by a newer overlay)

It also exits when the gateway stays unreachable past `AWM_RECONNECT_DEADLINE_S` (default 10s). The deadline counts from the first failure of the outage, not from the last inbound frame. An idle service receives no frames, so a deadline counted from the last frame let a single keepalive blip end it with zero retries. Only a confirmed inbound frame ends an outage, so an accept followed by an immediate close cannot restart the deadline.

A self-minted sid (empty `AWM_SERVICE_ID`) clears on every disconnect. The next loop re-registers and hits `409`, then `GiveUp`, against a live incumbent. A hub-assigned sid survives a disconnect, for respawn by sid. This is the backstop for a hard-killed gateway, where lifespan shutdown never runs.

### Duplicates and eviction

A second base registering under a name whose record holds a live lease gets `409` and stands down (`api/hub.py::service_register`). The incumbent is untouched. Takeover of a record with a dead lease replaces it in place. Respawn by sid skips `_register`.

Overlays follow last-connect-wins instead. An overlay register (`/hub/service/register` with `overlay=true`, or `/hub/shadow/register` for pages) goes through `registry.replace_overlays`. It pops every incumbent overlay, keeps the base, and installs the newcomer. Only a collision with the base name returns 409.

1. `lease.signal_evicted` stages a `(reason, evictor)` notice for each evicted overlay and sets its lease's disconnect event.
2. The held WS handler unwinds and reads the notice with `take_eviction`.
3. It closes with code `4410` and an `evicted by <origin>: a newer shadow connected` reason, clamped to 123 bytes.

On the client side, the gatewayclient adapter maps `4410` to `GiveUp(reason)`. The `awm dev shadow` lease holder (`cli._hold_one_lease`) raises `_ShadowEvicted` and tears the whole shadow stack down. The origin is `"<name> @ <worktree>"`, passed through `AWM_SERVICE_ORIGIN` for services and the `origin` field for pages.

### Graceful teardown

On SIGTERM or SIGINT the gateway drains its services in-band, force-kills stragglers, then clears the journal so the next boot bootstraps clean. `server._drain_services()` is the drain.

The gateway installs a loop-level signal override at lifespan startup, before uvicorn's own handler. It sends `{"kind":"shutdown"}` over each control WS while the WS is still open, and each service stands down, drops its lease and exits.

**CAUTION** This uvicorn registers `signal.signal(sig, server.handle_exit)`, not `loop.add_signal_handler`. A capture from `loop._signal_handlers` finds nothing. The override captures uvicorn's `Server` from `signal.getsignal(sig).__self__`, then installs `loop.add_signal_handler(sig, _on_signal)`, which drains and then sets `server.should_exit`. It works on uvloop.

A second signal force-exits. If the `Server` cannot be captured (TestClient, a non-main thread), the gateway falls back to a flag-only wrapper and drains from the lifespan-shutdown backstop. Force-kill is the backstop, not the mechanism. It fires only for a straggler whose lease is still held after about 8s, or whose process lives after its lease is gone.

### Crash respawn

An unexpected control-WS disconnect schedules a watchdog through `supervisor.schedule_disconnect_watchdog`:

1. It re-registers the record, so a quick self-reconnect is accepted.
2. It waits `_RECONNECT_WINDOW_S` (10s).
3. It respawns from the journal if the service is still silent.

The watchdog runs only when the gateway is not shutting down, the journal entry exists, the service is enabled and has not reconnected, and the breaker has not tripped. `awm services stop` drops the journal entry before it kills, so a deliberate stop is never respawned.

**CAUTION** Schedule through `schedule_disconnect_watchdog`, never `create_task(supervise_disconnect(...))`. One watchdog per service stops a burst of disconnects from becoming a burst of respawns.

Two independent 10s windows exist. The service's give-up deadline is service-side. The reconnect and respawn window is gateway-side.

### The crash-loop breaker

Every respawn path (boot reconcile, disconnect watchdog, the self-heal sweep) goes through `_respawn_from_journal`. `supervisor._note_respawn` counts respawns that did not reach ready, up to `_RESPAWN_BUDGET`. Reaching ready clears the count. `_RESPAWN_WINDOW_S` is only a slow decay.

The breaker counts failures, not respawns per unit time, because the respawn cadence varies. The watchdog, the 45s sweep and ticks skipped for a zombie all differ, and a slower crash loop once escaped a 300s window entirely.

Past the budget the gateway stops respawning, logs at ERROR, annotates the journal entry and shows `breaker-tripped` in `awm services list`. There is no auto-retry. `awm services start|restart` is the only way back, so a wedged service stays visibly wedged. The ERROR log is the only notification.

### The orphan reaper

`gateway_ops.reap_orphans` scans `/proc` for `awm.<svc>.hub_adapter` processes whose `AWM_HUB_URL` origin (`host:port`) matches this gateway and which hold no healthy lease. It kills them SIGTERM then SIGKILL through `supervisor.kill_pid_group`. It runs on a timer (`reap_loop`, under `spawn_supervised`) and on demand as `awm services reap [--dry-run]`. Both run the same code, so the dry run shows what runs unattended.

A lease is possession, not health. The reaper spares a holder only if:

- it has a ready control channel, or
- it is an overlay, or
- it took the lease inside `_READY_GRACE_S`.

A holder unready past the grace is a corpse, and reaping it frees a name a zombie is squatting. A process younger than the grace is spared too, because mid-registration it has no lease and no record. An unknown age counts as young. The origin check runs before any kill decision, so a prod sweep cannot touch a dev sandbox's children.

**CAUTION** Spare by identity first, then by process group, never by pid alone.

- A process whose `AWM_SERVICE_ID` has a ready control channel is serving this gateway and is never an orphan, whatever the registry's pid says. A respawn reuses the journaled `service_id` and reconnects without re-registering, so a record can outlive the pid it names. On 2026-07-28 it did, and the sweep killed the whole fleet every 120s.
- A service is a tree: `run.sh`, then `mamba run`, then `python -m awm.<svc>.hub_adapter`. Every process in it matches the scan and the registry knows one. A pid-keyed spare leaves a sibling that looks like an orphan, and the group kill takes the spared process with it.

Anything that acts on a service's pid has to think in groups, and treat the pid as the least durable key it has.

## Dev sandboxes

There is one hub origin per node: the gateway process. Its port depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd) | `7819` | `awm.service` |
| `projects/awm/dev/` | `7821` | `awm dev start` |
| `projects/awm/web-ui/` | `7831` | `awm dev start` |
| `projects/awm/web-backend/` | `7841` | `awm dev start` |
| `projects/awm/feat-dag/` | `7861` | `awm dev start`, port pinned in `.env` |
| `projects/awm/feat-gamebot/` | `7871` | `awm dev start`, port pinned in `.env` |
| any other scope | `7851` | `awm dev start` |

`run.sh` derives a port from the worktree dirname, so sandboxes run beside prod and each other. The two composition scopes pin their port in a gitignored `awm/gateway/dev/.env`, which `run.sh` sources before the dirname `case`. Substitute your sandbox port wherever this file says `:7819`.

The CLI targets `BASE_URL`, built from `AWM_PORT` (default `7819`, prod). `AWM_HUB_URL` goes to services and the CLI ignores it. `awm dev shadow --port` (default `7821`) picks the hub to shadow, so a shadow cannot land on prod by accident.

Start a sandbox with the `awm dev start` CLI, not the `dev_*` MCP tools.

- `start` always runs `awm/gateway/dev/run.sh` locally.
- `status`, `stop`, `restart` and `seed` route to prod's `/svc/dev`. They fall back to the local `run.sh` when prod's base answers `{"inert": true}`.
- The `dev_*` MCP tools hit prod's base with no local fallback. They return `inert` until a sandbox is up.

A running sandbox's `dev` service overlays `/svc/dev` on prod, and `awm dev status` then routes to your worktree.

Only the `dev` scope runs the shared sandbox. Other awm scopes (`comp-*`, `svc-*`, `web-*`, …) shadow the hub on `:7821` with `awm dev shadow --port 7821 …`. A second sandbox has a different port and none of dev's seeded state. If nothing is on `:7821`, ask the `dev` scope's agent to start it. `feat-dag` and `feat-gamebot` are the exception: each runs its own sandbox so its feature family never pollutes dev's state.

**CAUTION** `dev/run.sh` records the `mamba run` wrapper's PID in `dev.pid`, not uvicorn's. A bare `kill -TERM $(cat dev.pid)` hits the wrapper. Use `awm dev stop`, which signals the uvicorn child. In prod, `systemctl stop` signals `awm gateway serve` directly.

## External registrations

`awm gateway register --url|--dir` fronts an external upstream or a hand-built static dir. awm's own backends never use it. The commands hit `/hub/*` on the gateway origin:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/hub/register` | Register. Returns `service_id` and `lease_ws_path` |
| `WS`     | `/hub/lease/{service_id}` | Hold the lease. A disconnect evicts. |
| `GET`    | `/hub/services` | List registrations and lease state |
| `DELETE` | `/hub/services/{name}` | Force-evict by name |

All are unauthenticated. The gateway binds loopback only.

- A `kind=url` registration may pass `strip_prefix: true` (`--strip-prefix`). The gateway then forwards the path without its mount prefix and sends the prefix as `X-Forwarded-Prefix`. It is off by default because an upstream that expects the full path breaks.
- `kind=static` serves canonical paths only: a file at the exact path, or a directory's `index.html`. A miss is a 404, with no `Accept` fallback and no SPA shell. Prerender every route for deep-link refresh. Front an upstream with routes the server cannot list as `kind=url`.

## Gotchas

- **Prefix conflicts return 409.** `/hub` and `/hub/*` are reserved.
- **`AWM_WORKSPACE` and `AWM_HUB_URL` attach a process to a sandbox.** Without them the CLI uses global discovery and may target prod. The dev starter exports both for its children. Export them yourself when you shell out separately. `tr '\0' '\n' </proc/<pid>/environ | grep -E 'AWM_WORKSPACE|AWM_HUB_URL'` shows which hub a process uses.
- **Never run two gateways on one port.** Sandboxes run in parallel on distinct ports.
- **Use `gatewayclient.SupervisedSubscription` for every emit subscription.** When an emitter restarts, the gateway drops its subscribers from the fan-out table. Unless the proxy also closes the socket, the consumer waits forever on a connection whose keepalives still pass, because they only prove the gateway is alive. Three services once shipped the same naive loop and went deaf together. The helper reconnects, bounds staleness with a jittered idle deadline and reports `healthy`. Show `healthy` in the service's `status`.
- **Use `gatewayclient.spawn_supervised` for every long-lived background task.** A bare `asyncio.create_task` whose handle nobody reads leaves the service looking healthy with that capability gone if the task raised on its first line. The wrapper logs at ERROR and respawns. It treats a return as a defect, so a supervised loop must never exit. Check the shutdown flag and skip the tick.
- **A 502 from `/svc/<name>/fn/<fn>` is an application error.** `proxy.py` maps every `RpcError` to 502, so a healthy service answering `{"error":"no such note"}` looks like a broken one at the HTTP layer. The transport codes are 503 (control channel not open) and 504 (no reply in time). A stopped service returns 404, and its emit-WS upgrade returns 403. A frontend that treats `status >= 500` as disconnected flaps on a healthy service. Bounce the socket on 0, 503, 504 or a fetch `TypeError` only, and let the emit socket's close report a stopped service. `pages/notes/src/lib/collab.ts::isLinkError` does this. A stubbed test cannot catch it. Ask the running gateway what it returns.
- **A child that must outlive awm needs its own cgroup.** In prod, `systemctl restart awm` kills by control group, and every descendant inherits the cgroup however it forks or `setsid`s. Detaching defeats only signal-based teardown. Place a survivor outside `awm.service` with `systemd-run --user` in a transient unit, as `awm/services/claude-science` does. The inverse trap is `awm/services/cx`: a Claude Code background session inherits the cgroup and environment of whichever process first started the node's `claude daemon`. A service that starts one when no daemon is up donates awm's cgroup to every session on the node. cx refuses to seed or start a session unless a daemon is already running (`claudedaemon.launch`). The transient unit is only the backstop. Either failure is invisible in dev: the process is simply gone after the next restart.

## Data-layer internals

`awm.scopes.data_dvc` wires DVC into scopes. `PROTOCOLS.md` § *Data* and `AGENTS.md` § *Data* cover usage. `provision_scope_data` is the only entry point, and it alone decides between DVC wiring and the legacy shared symlink.

awm wires and DVC operates. Wiring is the cache path, the merge driver, the hooks and the mount list. `dvc add`, `dvc checkout`, `git commit` and `git merge` run unwrapped.

- **The opt-in is the checkout.** `is_dvc_repo(p)` asks whether the worktree tracks a `.dvc/config`. There is no config table, no flag and no conversion verb. `AWM_DATA_DVC=0` is the global kill switch.
- **The cache path goes in `config.local`, absolute.** A tracked relative path resolves against a different base in a non-scope checkout, and DVC then starts a second cache there without an error. `config.local` is untracked, so it is per-machine.
- **`cache.type = hardlink,symlink`.** There is one physical copy per machine. A materialised file is a hardlink to the cache object. Nothing here ever runs `chmod +w` on a file, because the write bit belongs to an inode every scope and every commit reads. `chmod_dirs_writable` touches directories only, as the rmtree fallback.
- **Hooks go in the common git dir, by hand.** `dvc install` writes to `<root>/.git/hooks`, and in a secondary worktree `.git` is a file. Every awm scope is a secondary worktree. A post-commit hook exists beside post-merge because a conflicted merge fires no post-merge hook, and that is when a human has just hand-edited a pin. Both hooks are shared across worktrees, so their `[ -d .dvc ] || exit 0` guard is load-bearing.
- **git ignores a hook's exit status.** A failing `dvc checkout` removes the old files before it finds it cannot install the new ones, and it cannot fail the merge. It leaves a sentinel that `data_status` and provisioning report.
- **An absent mount list means everything. An empty one means nothing.** Treating them alike pulls every cold chunk in the project onto disk.
- **`gc` never runs `dvc gc`.** `dvc gc -p` holds the repo lock of every listed worktree for its whole run and re-walks the shared history once per worktree. One dry run locked ~100 worktrees for eight hours. `data_gc` reads the pins itself, once per bare repo, and sweeps `files/md5` with no repo lock.
- **CAUTION** The grace window is the only race guard. `dvc add` writes cache objects before git can see any pin for them, so an unreferenced object younger than `GC_GRACE_DAYS` survives. Shortening the window shortens the time an unstaged pin is safe.
- **A missing `.dir` manifest blocks gc.** Its children cannot be told apart from garbage. Restore it from the archive, or name it in `accept_missing`. Every wired project must be named too: in `projects` to keep its data, in `exclude` to drop it.
- **Teardown guards uncommitted work, not content.** Deleting a worktree unlinks names, never bytes. Only uncommitted work dies, and `git status` covers data and code at once.

## Frontend

A shared component is a source folder imported by name, with no per-unit manifest. There is no npm workspace, no per-component `package.json` and no per-page `vite.config.ts`.

- **Components** live at `awm/ui_components/<name>/`: `src/*.svelte`, `.ts` and `.css`, plus a `src/index.ts` barrel for the public surface. Import them as `@awm/<name>` or a subpath such as `@awm/primitives/style.css`.
- **Pages** live at `awm/pages/<name>/`: `index.html`, `src/main.ts` (which calls `mount(App, …)`) and `src/App.svelte`, plus optional `src/lib/**`, `src/styles.css` and a one-line `prefix.txt` that overrides the default `/ui/<dirname>`. `pages/primitives-gallery` and `pages/ptt` are bare placeholders in the canonical shape.
- **Third-party deps** go in the root `awm/package.json` only.

Resolution is one rule in two places, and they must agree:

- The bundler: root `awm/vite.config.ts` aliases `@awm/<name>/<sub>` to `ui_components/<name>/src/<sub>` (subpath rule first) and `@awm/<name>` to `ui_components/<name>/src/index.ts`.
- The typechecker: `awm/tsconfig.json` `paths` carry the same `@awm/*` glob, so `tsc` and `svelte-check` follow cross-component imports without a build.

**CAUTION** Vite honors package `sideEffects` only under `node_modules`, and treats aliased first-party source as side-effectful. Without help, every barrel re-export would bundle. The root config's `build.rollupOptions.treeshake.moduleSideEffects` marks component `.ts` and `.svelte` source side-effect-free, while query-bearing virtual CSS modules and real `.css` keep their side effects. A page then bundles only what it imports, and a used component's `<style>` survives. Before you change this rule, diff a built `dist/` against a known-good build.

### Serve contract

A page's serve contract is its built `awm/pages/<name>/dist/` and an optional `prefix.txt`. Nothing in the serve path reads frontend source or config.

- Prod registers a `kind=page` base with `POST /hub/page/register` and a dir path (`registry.register_page`). The gateway serves it with StaticFiles at `/ui/<name>`.
- A shadow registers through `_shadow_page_target`, which reads `pages/<name>/dist` and `_read_prefix_txt`.

The build and the authoring file set can therefore change without touching the gateway.

### Build and shadow a page

Build from your own scope's `awm/`, not `projects/awm/dev`. Shadow from the scope root against the running dev sandbox.

```bash
cd /home/tony/agentic_workspace/projects/awm/<scope>/awm
npm install          # once per machine
npm run build        # every page with an index.html → awm/pages/<name>/dist/

cd /home/tony/agentic_workspace/projects/awm/<scope>
awm dev shadow --port 7821 pages/<name>
```

Visit `http://127.0.0.1:7821/ui/<name>/`. Ctrl-C pops the overlay and dev's base resumes. Shadow reads the built `dist/`, so build first.

## Federation

Read `FEDERATION.md` before you touch anything cross-node. The loopback gateway stays open and unauthenticated. A peer's services are reached directly on that peer's `httpsfront` edge over CA-verified TLS, with a node-signed token. The gateway is a directory (`peer_resolve`, `peer_providers`), not a router. A call that belongs to a peer comes back as the peer's address for the caller to dial.

- This is not the retired v0 federation (cr-sqlite replication, leader election, a `peers` registry). The git history of its deletion is not a guide to the current system.
- The gateway trusts `X-Awm-As` because the edge strips every inbound caller header (`X-Awm-As`, `X-Awm-Caller-Pid`, `X-Awm-Session-Pid`, `X-Awm-Peer-Redirect`) and stamps its own. A new edge mount must do the same. The board mount strips every `X-Awm-*` header and removes a mesh credential (a node token or the legacy bearer) before the request reaches the board. The board authenticates its own party bearer and must never hold a credential that is live elsewhere.
- A singleton is re-homed per node, not per call. `AWM_TWOFA_PEER`, `AWM_SOCIAL_PEER` and `AWM_SSH_SLOT_PEER` in `<workspace>/.awm/env` name the owning node. `gatewayclient.call_maybe_peer`, `call_sync_maybe_peer` and `subscribe_maybe_peer` are the single branch point. Use them even from sync code. A hand-rolled local POST is how one consumer borrows while another does not.

What is singular is the resource, not always the service:

- `2fa` is singular whole. A borrowing node may run it for local verbs but must not act as owner. Its `/approve` listener defaults off wherever `AWM_TWOFA_PEER` is set, because Duo's attempt budget is per account and two listeners spend it twice.
- `social` runs on every node, and only the identity is singular. Mark such an account `singleton = true` in `social.toml`. The owner (selector unset) connects it. A borrower does not connect it and forwards the verbs that name it. Getting this wrong once put two bots on one Discord token. It looked like a flaky slash command: Discord gives an interaction to one session, and the loser answers `10062 Unknown interaction`.

## File map

When you need something not mapped here, query the `graphify` domain (`find`, `refs`, `query`, `affected`) before an Explore agent. It indexes the deployed tree, so it lags uncommitted edits. Paths are under `awm/gateway/awm/gateway/`.

- **Service discovery** — `hub/discovery.py`.
- **Registry and kinds** — `hub/registry.py`: one `_stacks` dict per prefix, `replace_overlays`, `register_page`.
- **RPC layer** — `hub/rpc.py`: `ControlChannel` per service, the `_pending` call table, subscribers, sessions, bridge ids.
- **Service translator and bridge** — `hub/proxy.py`: `proxy_service_http`, `open_session_via_http`, `proxy_session_ws`, `proxy_service_emit_ws`.
- **Supervisor and journal** — `hub/supervisor.py`: `reconcile_journaled_services`, `bootstrap`, `spawn_service`, `kill_pid_group`, `supervise_disconnect`. State is at `<AWM_DIR>/state/services.json`.
- **Catalog** — `catalog.py`: `_tool_name`, `list_tools` and `dispatch` for the expanded surface, `list_domain_tools`, `_describe_domain` and `_dispatch_domain` for the collapsed one. The foreign gate (`foreign_grants`, `_dispatch_foreign`) and the tier split (`_tier_split`, `_more_tool`) live here. `mcp_more.py` holds the `more` rewrite, and `hub/discovery.py` reads `tier`.
- **Mode gate** — `server.py` (`_caller_mode`, `_call_refusal`, `_door_refusal`) applies the policies in `awm/service_components/config/awm/config/modes.py`. `awm/service_components/claudedaemon/awm/claudedaemon/sessionmode.py` answers `mode_of`.
- **Federation directory** — `peers.py` maps a name to its peer record (edge address, relation, swarm, role, pinned key, grants) and owns the writes. `awm.config.peerbook` is the one reader. `peer_catalog.py` maps a name to the domains of its domestic peers, holds the default-provider rules and raises `PeerRedirect` instead of relaying. `peer_files.py` turns `files[]` a peer returned into local copies over the peer's `/files` mount. It is its own module because both MCP proxies must call it, or the rollback proxy hands back paths that exist only on the peer.
- **MCP proxies** — `mcp_server.py` picks `mcp_stdio.py` (default) or `mcp_server_sdk.py` (`AWM_MCP_SDK=1`). `mcp_http.py` holds the timeout ladder both share.
- **Gateway control ops** — `gateway_ops.py`, generated through `operations.py`.
- **CLI** — `cli.py`. The `gateway` and `services` groups are generated from `GATEWAY_OPERATIONS`. `awm dev shadow` (search `dev_app`) and the page-shadow helpers (`_shadow_page_target`, `_read_prefix_txt`, `_post_page_register`) live there.
- **Frontend** — `awm/vite.config.ts`, `awm/scripts/build.sh`, `awm/package.json`.

## The editable install

The awm env's editable install (`…/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py`) maps `awm` to `/home/tony/agentic_workspace/awm`, the release tree. `import awm` from the env therefore loads release code, not your worktree's. Pick one workaround:

- Set `PYTHONPATH=<your-worktree>` to shadow the mapping.
- Spawn a subprocess with cwd and `sys.path[0]` pinned to your worktree.
- Merge into release, which moves the mapping's target. Deploy does this.

## Running tests

Each dist (the gateway and each feature service) owns its own `tests/` directory and `pyproject.toml` with its pytest config. All dists merge into the `awm` namespace, so one `pytest` over the tree cannot import them all. The runner invokes pytest once per dist, with that dist's source root and the shared components on `PYTHONPATH`:

```bash
awm/gateway/scripts/run-tests.sh                       # every dist. Run before merging
awm/gateway/scripts/run-tests.sh scopes gateway        # named dists only
PYTEST_ARGS="-x -q" awm/gateway/scripts/run-tests.sh   # extra pytest args
```

It reports pass or fail per dist and exits non-zero if any failed.

- Make cross-dist imports in a test lazy, inside a fixture or function. A top-level cross-dist import brings back the namespace shadowing the runner avoids.
- Add a new service with tests to both `DISTS` and `ORDER` in the runner. A dist missing from `DISTS` reports `unknown dist` and never runs. `reflection` sat that way for months.
