# AWM Internal Architecture and Operation

*Reference for agents working on awm itself, and for anyone operating the workspace: the service architecture, and the procedures for creating scopes, moving work between them, and managing data. Auto-injected only when the agent's cwd contains this file at its root; every other agent Reads it by path at the moment it needs a procedure.*

For the orientation every scope agent receives at startup, see `WORKSPACE.md`.

## Architecture overview

awm is a modular **gateway** plus a set of out-of-process **feature services**. The gateway (`awm/gateway/`, package `awm.gateway`) is the sole interface (CLI, HTTP, MCP stdio) and the coordination hub; it owns no tables of its own and boots standalone. Feature surfaces appear as their modules register.

The tree is a nest of pip dists under `awm/`, all merging into the PEP 420 `awm` namespace:

- **Gateway** — `awm.gateway`: the interface + hub. Discovers, bootstraps, and supervises services; renders their APIs onto MCP/HTTP/CLI.
- **Shared Python components** (`awm/service_components/`, pure imported source, no `install.sh`) — `awm.config`, `awm.persistence`, `awm.gatewayclient`, `awm.agentcore`.
- **Frontend source** (`awm/ui_components/` + `awm/pages/`) — shared Svelte components imported by name (`@awm/<name>`) and the static page bundles that compose them (see § *Frontend component system*).
- **Feature services** (`awm/services/<name>/`, each its own dist + DB + `run.sh`) — the live set is whatever `awm services list` reports; each folder's `INSTALL.md` is its contract. Two facts you can't read off the directory: `skills` is retired (debrief is a native CC skill; the rest of the catalog is reference-only on disk), and `compute` is the one service that acts on processes it does not own — it kills and renices agent-launched jobs, so anything new that agents *launch* and must not lose (a long-lived server, a tunnel, an MCP shim) has to be added to its `PROTECTED` list.

Each feature service owns its own SQLite DB; there is no shared `state.db`. Cross-service references are **natural keys** (an agent = its `(project, scope)` pair) validated by calling the owning service over gateway RPC, never by importing it. `scopes` owns identity and exposes it via the RPCs frozen in `awm/services/scopes/IDENTITY_CONTRACT.md`.

**`agentcore` — the pluggable harness layer.** `awm.agentcore` (a leaf imported-source component — zero `awm.config`/gateway imports) owns "talk to a CLI agent harness and yield normalized events," and nothing about scopes or the gateway. `open_agent(AgentConfig{harness:'claude'|'opencode', mode:'live'|'oneshot', model?, params?, permissions='full', workdir?, resume_id?, mcp_config?, system_prompt?}) -> AgentSession` (`subscribe()`/`send()`/`close()`); `run_once(config, prompt, schema?)` is the one-shot wrapper. Both harnesses are **subprocess** drivers — claude (`--print` stream-json, `--permission-mode=bypassPermissions`) and opencode (warm `opencode serve`, `--dangerously-skip-permissions`), no Agent SDK, no OpenRouter — mapped into one `AgentEvent{id, kind:'message'|'partial'|'tool_use'|'tool_result'|'status'|'result'|'error', text?, data?, ts}`; `id` is the dedupe/cursor key end-to-end. The agents service builds its sessions through it. (`stt`'s convo mode no longer uses agentcore — its former LLM "refiner" was removed; convo now accumulates the raw whisper transcript and auto-submits on measured silence.)

**A scope IS the channel.** There are no `rooms`/`messages`/`session_logs` tables. One `scope_posts` table (kind = `message`/`journal`/`system`/…) + `scope_subscribers`, addressed by `(project, scope)`, carries everything; non-agent inboxes (`user:`/`project:`/`workspace`) are non-literal channels (`owner_project=''`). The surface is `scope_post` / `scope_fetch` / `scope_subscribe` / `scope_unsubscribe` (in `awm/services/scopes/awm/scopes/channel.py` + `operations/scope_channel.py`). The chat data-flow is **asymmetric**: the frontend POSTs human messages to the scope (`scope_post`) and SUBSCRIBES to the agents service for output; the agents service subscribes to its scope's channel to feed the agent's stdin and records the agent's per-turn acts in `agent_transcript` (exposed live over the agents `transcript` direct WS — backfill-from-cursor + live push, de-duped by act `id`), **not** auto-posted back to the scope. Only deliberate agent messages (e.g. a debrief) are an explicit `scope_post`.

## Service Hub

`awm.gateway.server:app` is the gateway — the sole interface + coordination hub, owning no tables of its own. A few path prefixes are *registered* at runtime; matched requests dispatch to one of four kinds:

| Kind | Surface | Typical caller |
|---|---|---|
| `service` | RPC-over-WS at `/svc/<name>` | a folder under `awm/services/<name>/` discovered + run by the gateway |
| `page` | static bundle at `/ui/<name>` | a built bundle under `awm/pages/<name>/dist/` |
| `url` | HTTP/WS proxy at any prefix | external services registered via `awm gateway register --url ...` |
| `static` | static bundle at any prefix | external bundles registered via `awm gateway register --dir ...` |

Each prefix maps to a stack of records (a base + at most one live overlay). Overlays are pushed by `awm dev shadow`. **Last connect wins:** a new overlay *evicts* any incumbent overlay on the prefix (it never stacks LIFO, and a duplicate overlay name no longer 409s) — the evicted shadow is closed with WS code `4410` carrying an `evicted by <who>: <why>` notice. The base is never evicted; its traffic resumes instantly when the overlay's lease closes.

### Feature services — the `awm/services/<name>/` contract

The first-class way to add a backend is a folder under `awm/services/<name>/` with a self-contained executable `run.sh`. The gateway **discovers** it by filesystem scan (`awm/gateway/awm/gateway/hub/discovery.py`), **bootstraps** it on first boot, respawns it durably, and injects **exactly three** env vars (`AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_SERVICE_ID`) — no auth, ever. Enable/disable state lives in `.awm/services/enabled.json` (a service absent from the file is enabled); the operator surface is `awm services list|start|stop|restart|enable|disable|reap [name|--all]`. The full human-facing authoring contract is **README § *Authoring a service***.

Foundation facts for anyone touching this:

- **Per-service SQLite DBs, no shared `state.db`.** `awm.persistence.databases` is the factory — `get_connection(service)` + `init_service_db(service, schema_sql, schema_version=…)` give each service its own DB at `AWM_DIR/services/<svc>/<svc>.db`. Raw SQL lives behind `awm.persistence.dao.BaseDAO` subclasses shipped per-service; each service self-seeds via its own `seed.py`. Per-service schema + legacy seed shapes are documented in `awm/service_components/persistence/SCHEMA_HANDOFF.md`.
- **Services are OUT-OF-PROC**, spawned + PID-journaled by the supervisor; the control-WS lease is liveness. Each service is a self-contained `run.sh` (the only thing the gateway runs — `bash run.sh`) plus a tiny `hub_adapter.py` built on **`awm.gatewayclient.ServiceAdapter`** (the reusable register→ready→serve→dispatch→reconnect base). `awm.gatewayclient` also carries `call`/`call_sync`/`RefCache` for the service→service direction. The `/svc/*` control plane is unauthenticated (loopback only). `awm/services/discord/` is the worked example to copy.
- **Install/iterate each module via its own `install.sh`, never hand-rolled `pip`.** `awm/gateway/install.sh` is the composition root — it installs `config` + `persistence` and every feature dist `--no-deps`, then the gateway itself (resolving third-party deps). Imported-source components (`config`, `persistence`, `gatewayclient`, `agentcore`, `ui_components`) have no `install.sh`. Override the target env with `AWM_ENV`.
- **Registration contract.** A service declares its API as a serializable `ready.api` manifest (`functions`/`emitters`/`subscriptions`); that threads into the MCP/CLI/HTTP generation layer (manifest → `Operation` → the `operations.py` compiler; dispatch is catalog-owned). The hub-mediated service↔service comms model — `call` request/reply, `emit`/`sub` pub/sub, `Bridge` streaming, all identity-aware via `as_`, never direct sockets — is documented in `awm/gateway/awm/gateway/catalog.py`. The catalog is the single live source the gateway renders to all three surfaces; `/tools` and `/invoke` read from it. A manifest function may carry an optional `"tool"` key to set its exact MCP-surface name, decoupling the projected label from the internal op `name` used for RPC dispatch — so the surface reads as clean `<domain>_<verb>` (`scope_create`, `agent_spawn`, `scope_resolve`) while internal names (incl. the frozen `IDENTITY_CONTRACT` camelCase RPCs) are unchanged. `_tool_name` honors it; `list_tools` warn-and-skips duplicate projected names (override names must be globally unique).
- **Dual `/tools` projection — expanded vs collapsed-by-domain.** `catalog.list_tools()` is the **expanded** per-verb surface (~71 tools); the CLI generator (`register_service_cli_commands`) and the flat `/invoke` by-name dispatch both depend on it, so it stays. Alongside it, `catalog.list_domain_tools()` folds that same surface **by domain** — the projected name split on the first underscore (`scope_create` → domain `scope` / verb `create`; native ops fold by `cli_group`/`cli_command`) — into one generic `{verb, args}` tool per domain (~8–10 total). `GET /tools?view=domains` returns the collapsed view; the **MCP stdio proxy requests it** (so every MCP client, including non-deferring spawned agents + OpenCode, carries the tiny surface), while the default `GET /tools` and the CLI/HTTP surfaces stay expanded. Adding **`&peers=1`** widens that same collapse to the fleet — still one tool per domain name, but peer-only domains appear and the envelope's third key `peer` chooses the node (see FEDERATION.md § *Cross-peer calls*). It is opt-in for a reason: the plain view is what a *peer* fetches from this node's edge, and it must stay local-only or the fleet advertises transitive peers nobody can dial. `dispatch()` gained a domain branch *ahead* of the flat branches: when `name` is a known domain and `args` carries `verb`, it routes to `_dispatch_domain` — `verb="describe"` is answered from the catalog (no service round-trip; `describe` is a **reserved verb** on every domain), a native verb runs its `Operation`, and a service verb is resolved back to its internal fn via the existing `_find_service_fn(f"{domain}_{verb}")` reverse lookup (so a name≠tool divergence like `scope_refresh`→`awm_refresh` still routes) and RPC'd with `as_` threaded as before. The collapse is **purely additive** in the gateway — flat dispatch, the default `/tools`, and all CLI/HTTP generation are untouched, so it's reversible by reverting the proxy's `?view=domains` request. A domain's verbs must be unique (folding warn-and-skips dups, first-wins) — keep new `"tool"` overrides under a real `<domain>_<verb>` shape (a bare single-token override becomes its own junk single-verb domain, and a **two-word service name splits at its first underscore** — `claude-science` projecting `claude_science_status` lands as domain `claude` / verb `science_status`, so such a service must pick a one-token domain: `science_status`, giving `awm science status`). A manifest function may carry `"surfaces": ["cli","http"]` (default = all three when omitted) to keep a verb **off the MCP surface**: `_domain_catalog` skips non-`mcp` functions and `_dispatch_domain` rejects them, so `writing(verb="add")` is unknown/404 over MCP while the CLI's flat `/invoke {name:"writing_add"}` (via `_find_service_fn`, unfiltered) still works — the mechanism behind CLI-only write verbs (`awm/services/writing`). A function's `"timeout"` is now honored on the `/invoke`/domain path too (via `_rpc_call` → `_fn_timeout`), not only the `/svc/<name>/fn/<fn>` proxy; the generated service CLI dispatch uses a 600s client ceiling so a slow verb isn't client-aborted at 30s.
- **Server-side verb gating for placed agents.** Because the placement toolset collapsed onto the single `agent` domain tool (`mcp__awm__agent`), claude's `--allowedTools` can no longer scope *which* placement verb a placed agent calls (one tool, all verbs) — and the default OpenCode harness ignores `--allowedTools` entirely. So the agents service gates **server-side**: `placement.VERB_PROFILES` is the per-mode verb allowlist, recorded as `allowed_verbs` on the `agent_instances` row at spawn, and `placement.ensure_verb_allowed(as_, verb)` (called in the `hub_adapter` relay wrappers, keyed on the `X-Awm-As` identity) rejects a disallowed verb regardless of harness (`task_fail` always allowed). `--allowedTools` now carries only filesystem built-ins + `mcp__awm__agent`. This is **defense-in-depth / a guardrail, not a trust boundary** — `X-Awm-As` is plaintext + spoofable on the unauthenticated loopback bus (an agent with `Bash` can curl `/invoke` and forge any identity); the real fix (a per-placement bearer secret, the `placement_token` already exists) is a deferred follow-up.
- **The gateway's own control plane runs through the same compiler.** Its status/restart/mcp-sync, hub list/deregister, and services-lifecycle ops are declared once as `GATEWAY_OPERATIONS` (a list of `operations.Operation` in `awm/gateway/awm/gateway/gateway_ops.py`); the three generators in `operations.py` (`operations_to_mcp_tools`, `register_fastapi_routes`, `register_cli_commands`) project them onto MCP (via `catalog.list_tools`/`dispatch`), HTTP (`server.py` startup), and CLI (`cli.py`). A new gateway control op is one `Operation`, never a hand-rolled CLI command + HTTP route + native MCP tool in three places.
- **Concurrency.** One server loop owns all async hub state; blocking work is offloaded (`run_in_threadpool` / `run_in_executor`); `asyncio.run()` is banned inside the daemon (survives only in the CLI/MCP-proxy/entrypoint processes). `catalog.dispatch` is async, `/tools` is sync.

The `url` / `static` kinds and the `awm gateway register --url|--dir` lease commands remain only for **external** bundles/services (a remote HTTP/WS upstream, or a hand-built static dir) — not for awm's own backends.

### Service lifecycle

The gateway↔service lifecycle covers all start/stop/crash directions deterministically; editing a backend never leaks orphans.

- **First-boot bootstrap (reconcile-then-bootstrap).** Boot doesn't depend on a populated journal. The gateway (1) `reconcile_journaled_services()` for anything already journaled, then (2) **bootstraps** any discovered-but-unjournaled enabled service — so a fresh clone or a wiped `.awm/state/services.json` comes up with every enabled service running. Discovery is a filesystem scan of `awm/services/*` for `run.sh`; no hardcoded name list, no manifest-sync step. A disabled service (per `.awm/services/enabled.json`) stays down across restarts.
- **No dev auto-reload.** `awm/gateway/dev/run.sh` launches uvicorn **without** `--reload`/`--reload-dir`, so saving a backend file never swaps the worker out from under its spawned services. Backend edits require an explicit `awm dev restart`.
- **Service-side give-up (`gatewayclient/adapter.py`).** `ServiceAdapter.run()` exits 0 (no retry) on a stand-down signal — a `409` register, a control-WS close `4409` (lease held) / `4404` (unknown), an upgrade `409`/**403** (the gateway closes an unknown `service_id` *pre-`accept()`* with `close(4404)`, which the `websockets` client surfaces as HTTP **403** on the upgrade), or an in-band `{"kind":"shutdown"}` frame — via a `GiveUp` sentinel. It also exits once the gateway has been unreachable past `AWM_RECONNECT_DEADLINE_S` (default 10s, env-tunable), measured from the last **confirmed-up** disconnect (a transient blip still retries with backoff). The per-loop `last_up` deadline clock is refreshed only on a confirmed inbound frame, so an accept-then-immediately-close can't keep resetting it. A **self-minted** sid (empty `AWM_SERVICE_ID`) is cleared on every disconnect so the next loop re-registers and deterministically hits `409 → GiveUp` against a live incumbent; a hub-**assigned** sid (respawn-by-sid) is kept. This is the backstop for a hard-killed gateway, where lifespan shutdown never runs.
- **Duplicate rejection — bases only (`api/hub.py::service_register`).** A second *base* instance registering under a name whose record still holds a **live** control-WS lease gets `409` and stands down; the incumbent is untouched. Takeover of a **dead** (lease-not-held) record still replaces in place. Respawn-by-sid skips `_register` entirely. (This is the base-takeover guard; *overlays* follow last-connect-wins instead — see next.)
- **Shadow eviction — last connect wins (`registry.replace_overlays` + `lease.signal_evicted`).** An overlay register (`/hub/service/register` with `overlay=true`, or `/hub/shadow/register` for pages) routes through `registry.replace_overlays`, which pops every incumbent overlay on the prefix (keeping the base) and installs the newcomer as the sole overlay — so a duplicate overlay name evicts the incumbent rather than 409ing (the old `PrefixConflict`-on-duplicate-name is gone; only a collision with the **base** name still 409s). Each evicted overlay is staged a `(reason, evictor)` notice via `lease.signal_evicted`, which sets its lease's disconnect `Event`; when the held WS handler unwinds it reads the notice with `take_eviction` and closes with code **`4410`** + a 123-byte-clamped `evicted by <origin>: a newer shadow connected` reason. Client side: the `gatewayclient` adapter maps `4410 → GiveUp(reason)` (logged by `_run_target`); the `awm dev shadow` page-lease holder (`cli._hold_one_lease`) parses it and raises `_ShadowEvicted` to tear the whole shadow stack down. The "who" (`origin`) is `"<name> @ <worktree>"`, threaded from the CLI via `AWM_SERVICE_ORIGIN` (services) / the shadow-register `origin` field (pages).
- **Graceful teardown (`server.py`).** On SIGTERM/SIGINT the gateway drains its services **in-band**, then force-kills any straggler, then clears the journal so the next boot bootstraps clean. The drain is one reusable `server._drain_services()` coroutine. In-band delivery works via a **loop-level signal override** installed at lifespan startup that takes ownership of SIGTERM/SIGINT *before* uvicorn's own handler, so the gateway delivers the `{"kind":"shutdown"}` frame over each live control WS **while it is still open** — each service stands itself down, drops its lease, and exits. The override captures uvicorn's `Server` off `signal.getsignal(sig).__self__` — **this uvicorn (0.x) registers `signal.signal(sig, server.handle_exit)`, NOT `loop.add_signal_handler`**, so a `loop._signal_handlers` capture finds nothing; `getsignal` is the correct seam — then installs `loop.add_signal_handler(sig, _on_signal)` (works on uvloop too) which drains then flips `server.should_exit`. A second signal force-exits (operator escape hatch). If the Server can't be captured (TestClient / non-main-thread) it falls back to a flag-only wrapper and the drain runs from the lifespan-shutdown backstop. **Force-kill is the backstop, not the primary mechanism** — it fires only for a straggler whose lease is still held after the ~8s grace window, or whose process is alive while its lease is already gone.
- **Crash-respawn watchdog (`supervisor.schedule_disconnect_watchdog` + `hub.py` disconnect hook).** An unexpected control-WS disconnect schedules a watchdog: re-register the record (so a quick self-reconnect is accepted), wait `_RECONNECT_WINDOW_S` (10s), respawn from the journal if still silent. Gated on not-shutting-down + journal-entry-present + enabled + not-reconnected + breaker-not-tripped. `awm services stop` drops the journal entry **before** killing, so a deliberate stop is never respawned. Two independent 10s windows exist — the service's give-up deadline and the gateway's reconnect/respawn window (service-side vs gateway-side authority). **Schedule through `schedule_disconnect_watchdog`, never `create_task(supervise_disconnect(...))`** — one watchdog per service at a time is what stops a burst of disconnects becoming a burst of respawns.
- **Respawn is bounded — the crash-loop breaker (`supervisor._note_respawn`).** Every respawn path (boot reconcile, disconnect watchdog, self-heal sweep) funnels through `_respawn_from_journal`, which counts **respawns that did not reach ready** (`_RESPAWN_BUDGET`; reaching ready clears the count, and `_RESPAWN_WINDOW_S` is only a slow decay valve). Counting per unit time instead ties the bound to the respawn cadence — and the cadence varies (watchdog vs 45s sweep, ticks skipped because the corpse is a zombie), so a *slower* crash loop escaped a 300s window entirely. Past the budget the gateway **stops respawning**, logs at ERROR, annotates the journal entry, and shows `breaker-tripped` in `awm services list`. There is deliberately **no auto-retry**: `awm services start|restart` is the only way back, so a wedged service stays visibly wedged instead of flapping. A previously self-recovering (if noisy) failure now needs an operator — that is the point, and the ERROR log is the only notification.
- **Orphan reaper (`gateway_ops.reap_orphans`).** Scans `/proc` for `awm.<svc>.hub_adapter` processes whose `AWM_HUB_URL` origin (`host:port`) matches this gateway and which hold no *healthy* lease, then SIGTERM→SIGKILL via `supervisor.kill_pid_group`. Runs both on a timer (`reap_loop`, started under `spawn_supervised`) and on demand as `awm services reap [--dry-run]` — same code, so the dry run shows exactly what runs unattended. **A lease is possession, not health:** a holder is spared only if it has a ready control channel, is an overlay, or took the lease inside `_READY_GRACE_S`. A holder unready past that grace is a corpse and is reaped, which is what unsticks a name whose slot a zombie is squatting. Processes younger than the grace are spared too (mid-registration they have no lease and no record); an unknowable age counts as young — the reaper never guesses toward killing. The origin check runs before any kill decision, so a prod sweep can't touch a dev sandbox's children. **Sparing is by *identity* first, then by process *group* — never by pid alone.** A process whose `AWM_SERVICE_ID` has a ready control channel is serving this gateway right now and is never an orphan, whatever the registry's pid bookkeeping says; that check is the invariant, because a respawn reuses the journaled `service_id` and reconnects *without* re-registering, so a record can outlive the pid it names (2026-07-28: it did, and the sweep killed the whole fleet every 120s until the record was made to follow the respawn). Group-widening then covers the rest of the tree (`run.sh` → `mamba run` → `python -m awm.<svc>.hub_adapter`): every process in it matches the scan and the registry knows only one, so a pid-keyed spare leaves the sibling looking like a textbook orphan and the group kill takes the spared process down with it. Same tree shape as the `dev.pid` wrapper trap below; anything that acts on a service's pid has to think in groups — and treat the pid as the *least* durable key it has.
- **The ready-ASAP contract (`gatewayclient/adapter.py`).** A service **registers and sends its `ready` frame before running `on_start`**, then keeps initialising in the background. Inbound `call` / `notify` / `session.open` envelopes that arrive during initialisation **buffer** on an event (bounded by `AWM_INIT_WAIT_S`, default 60s) rather than failing, so a caller sees a slow first call instead of an error — and because `on_start` is where `init_service_db` runs, that gate is what keeps a handler off a half-built database. A hung init surfaces as a "still initialising" error; a failed one propagates out of `run()` so the process exits rather than sitting there looking healthy. **Do not await slow work before `run()`** — the reaper above treats a prolonged unready state as evidence the process is broken, so "unready" has to mean *broken*, not *still loading*. Long-running startup work belongs in a task `on_start` spawns, not in `on_start` itself.

**Starting a dev sandbox: use the `awm dev start` CLI, NOT the `dev_*` MCP tools.** `start` always execs `awm/gateway/dev/run.sh` locally (it bootstraps the very sandbox that would serve it); `status`/`stop`/`restart`/`seed` route to prod's `/svc/dev` and fall back to local `run.sh` when prod's base answers `{"inert": true}` (no sandbox shadowing it yet). The `dev_*` **MCP** tools hit prod's base with **no** local fallback, so they just return `inert` until a sandbox is up — don't reach for them to start one. Once a sandbox is running, its `dev` service overlays `/svc/dev` onto prod, so `awm dev status`/etc. then route to your worktree. Only the **`dev`** scope runs the shared sandbox; most awm scopes (`comp-*`/`svc-*`/`web-*`/…) shadow the already-running hub at `:7821` (`awm dev shadow --port 7821 …`) instead of starting a second one — a second sandbox is a different port with none of dev's seeded state. If nothing is on `:7821`, ask the `dev`-scope agent to start it. The two **composition scopes** — `feat-dag` (conversational agents + voice + web-ui + DAG orchestration) and `feat-gamebot` (web game bots) — are the exception: each runs its OWN isolated sandbox (`:7861` / `:7871`, pinned per-worktree via a gitignored `awm/gateway/dev/.env`) so its feature family never pollutes dev's seeded state.

dev caveat: `dev/run.sh` records the **mamba-run wrapper** PID in `dev.pid`, so a bare `kill -TERM $(cat dev.pid)` hits the wrapper, not uvicorn — `awm dev stop` (which `pkill -P`s the uvicorn child) and prod `systemctl stop` (systemd signals the `awm gateway serve` process directly) both deliver the clean signal that triggers the in-band path.

### Hub origin = gateway port

There's only ever one hub origin per node — the gateway process. Which port depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd-managed) | `7819` | `awm.service` on the host |
| Dev sandbox: `projects/awm/dev/` | `7821` | `awm dev start` |
| Dev sandbox: `projects/awm/web-ui/` | `7831` | same |
| Dev sandbox: `projects/awm/web-backend/` | `7841` | same |
| Composition sandbox: `projects/awm/feat-dag/` | `7861` | `awm dev start` (port pinned via gitignored `.env`) |
| Composition sandbox: `projects/awm/feat-gamebot/` | `7871` | same |
| Dev sandbox: any other scope (fallback) | `7851` | same |

Per-scope port bands are derived from the worktree dirname, so dev sandboxes run side-by-side with prod (and each other). The two composition scopes (`feat-dag`/`feat-gamebot`) instead pin their port explicitly in a gitignored `awm/gateway/dev/.env`, which `run.sh` sources *before* the dirname `case` — so they own a stable isolated port without editing the tracked `case` block. **Substitute your sandbox port** whenever a section says `:7819`. The CLI's own target hub is `BASE_URL`, computed from `AWM_PORT` (default `7819` = prod) — `AWM_HUB_URL` is injected into *services*, not consulted by the CLI. `awm dev shadow` takes a `--port` (default `7821`) to pick which hub it shadows onto, so you can't fat-finger a shadow onto prod.

### External registrations (`kind=url` / `kind=static`)

For an **external** upstream the gateway should front, register a lease via `awm gateway register`. These POST/DELETE/WS to `/hub/*` on the gateway origin:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/hub/register` | Register a service; returns `service_id` + `lease_ws_path` |
| `WS`     | `/hub/lease/{service_id}` | Hold lease; disconnect → eviction |
| `GET`    | `/hub/services` | List registrations + lease state |
| `DELETE` | `/hub/services/{name}` | Force-evict by name |

All unauthenticated — the gateway binds loopback only. A `kind=url` registration may pass `strip_prefix: true` (CLI: `--strip-prefix`) to forward the path with its mount prefix removed and announce that prefix as `X-Forwarded-Prefix` — for an upstream that serves at the root and rebases its own links from that header; it is off by default because an upstream expecting the full path would break. `kind=static` serves canonical paths only (a file at the exact path, or a directory's `index.html`; a miss is a 404 — no `Accept` fallback, no SPA shell synthesis). For deep-link refresh in a SvelteKit/React-Router bundle, prerender every route; for routes the server can't enumerate, front the upstream as `kind=url`.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix. `/hub` and `/hub/*` are reserved.
- **`AWM_WORKSPACE` + `AWM_HUB_URL` attach to a sandbox, not prod.** Without them, the CLI hits global discovery and may target prod `:7819`. The dev starter exports both for its children; if you shell out separately, export them yourself. To check which hub a running process was set up against: `tr '\0' '\n' </proc/<pid>/environ | grep -E 'AWM_WORKSPACE|AWM_HUB_URL'`.
- **Never run two gateways on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are how dev parallelism works.
- **Never hand-roll an emit-subscription loop — use `gatewayclient.SupervisedSubscription`.** A subscriber's socket and the emitting service's control channel are two things that must agree: when the emitter restarts, the gateway drops the subscriber from the fan-out table, and unless the proxy also closes the socket the consumer waits forever on a connection that looks perfectly healthy (keepalives still pass — they only prove the *gateway* is alive). Three services shipped byte-identical copies of the same naive loop and all three went permanently deaf together. The helper reconnects, bounds every unmodelled staleness class with a jittered idle deadline, and reports `healthy` — surface that in the service's `status` so deafness is visible before something urgent depends on it.
- **Never hand-roll a long-lived background task either — use `gatewayclient.spawn_supervised`.** `self._x_task = asyncio.create_task(self._x())` and then never reading `_x_task` leaves a service running, apparently healthy, with that whole capability silently absent if the task raised on its first line. The wrapper logs at ERROR and respawns. It treats a *return* as a defect too, so a supervised loop must never exit — check the shutdown flag and skip the tick instead of breaking out.
- **A slow `on_start` is a bug now, not just a smell.** See the ready-ASAP contract above: the gateway reaps a lease-holder that stays unready, so startup work that takes real time belongs in a task, and anything a caller needs must be behind the adapter's init gate rather than raced against it.
- **A 502 from `/svc/<name>/fn/<fn>` is an *application* error, not a dead upstream.** `proxy.py` maps every `RpcError` — i.e. any error envelope a service replies with — onto 502, so a healthy service answering `{"error":"no such note"}` is indistinguishable at the HTTP layer from a broken one. The transport-shaped codes are 503 (control channel not open) and 504 (no reply in time); a *stopped* service is a 404 and its emit-WS upgrade a 403. A frontend that keys "am I connected?" off `status >= 5xx` therefore flaps on a perfectly healthy service — bounce the socket on 0/503/504 and a raw fetch `TypeError` only, and let the emit socket's close report a stopped service (`pages/notes/src/lib/collab.ts::isLinkError`). A stubbed test cannot catch this; ask the running gateway what it returns.
- **A child that must outlive awm needs a different cgroup, not just a detach.** Where the gateway runs under systemd (prod), `systemctl restart awm` kills by control group, and a cgroup is inherited by every descendant however it forks, `setsid`s or double-forks. Detaching defeats signal-based teardown and nothing else. A service spawning something meant to survive a deploy has to place it outside `awm.service` — `systemd-run --user` into a transient unit is the mechanism `awm/services/claude-science` uses. The failure is invisible in dev and silent in prod: the process simply is not there after the next restart.

## Project data layer (`awm.scopes.data_dvc`)

Data is versioned by the same commit that versions the code: `data/<chunk>.dvc`
is a tracked ~110-byte pin, the bytes live once in `<workspace>/data/.dvc_cache`,
and `provision_scope_data` is the *only* entry point — one place decides between
DVC wiring and the legacy shared symlink.

**awm wires, DVC operates.** Wiring is the cache path, the merge driver, the
hooks, and the mount list; `dvc add` / `dvc checkout` / `git commit` /
`git merge` are used unwrapped. Design facts worth knowing before you touch it:

- **The opt-in is the checkout.** `is_dvc_repo(p)` is "does this worktree track
  a `.dvc/config`?" There is no config table, no flag to keep in sync, and no
  conversion verb — a project that has one gets wired on its next scope creation
  or heal; one that does not keeps the shared symlink. `AWM_DATA_DVC=0` is the
  global kill switch.
- **The cache path goes in `config.local`, absolute, never the tracked config.**
  A tracked relative path resolves against a different base in a non-scope
  checkout, and DVC then silently starts a *second* cache there rather than
  erroring. `config.local` is untracked, so it is per-machine by construction.
- **`cache.type = hardlink,symlink`.** One physical copy per machine; a
  materialised file is a hardlink to the cache object. That is why nothing here
  ever `chmod +w`s a *file*: the write bit belongs to an inode every other scope
  and every historical commit reads through. `chmod_dirs_writable` touches
  directories only, and exists solely as the rmtree fallback.
- **Hooks go in the common git dir, by hand.** `dvc install` builds its hooks
  path as `<root>/.git/hooks`, and in a secondary worktree `.git` is a *file* —
  every awm scope is a secondary worktree. post-commit exists as well as
  post-merge because a *conflicted* merge fires no post-merge hook at all, which
  is exactly when a human has just hand-edited a pin. Both are shared across
  worktrees, so `[ -d .dvc ] || exit 0` is load-bearing.
- **git ignores a hook's exit status.** A failing `dvc checkout` removes the old
  files before discovering it cannot install the new ones, and cannot fail the
  merge — so it leaves a sentinel that `data_status` and provisioning surface.
- **An absent mount list means "everything"; an empty one means "nothing."**
  Collapsing the two drags every cold chunk in the project onto disk.
- **`gc` is a guard, not a wrapper.** The cache is shared workspace-wide, so
  `data_gc` must be told every project whose data survives, defaults to dry-run,
  and refuses `all-branches`. Note dvc's own output says "Removed N objects"
  even for a dry run — trust the `dry_run` field in the reply.
- **Teardown guards uncommitted work, not content.** Deleting a worktree unlinks
  names, never bytes. What dies is what was never committed — and under one
  lever that is `git status`, covering data and code at once.

## Frontend component system

The frontend mirrors the Python imported-source model: a shared component is **just a source folder**, imported by name with no per-unit manifest and no hand-maintained dependency list. There is no npm workspace, no per-component `package.json`, no per-page `vite.config.ts`.

- **Components** live at `awm/ui_components/<name>/` — `src/*.svelte`/`.ts`/`.css` plus a `src/index.ts` barrel that re-exports the public surface. Nothing else. They're imported as `@awm/<name>` (and subpaths like `@awm/primitives/style.css`).
- **Pages** live at `awm/pages/<name>/` — `index.html` + `src/main.ts` (calls `mount(App, …)`) + `src/App.svelte`, plus optional `src/lib/**`, `src/styles.css`, and a one-line `prefix.txt` to override the default `/ui/<dirname>` prefix. Nothing else. (`pages/primitives-gallery` and `pages/ptt` are bare source-only placeholders — the canonical shape.)
- **Resolution** is one rule, in two places that agree:
  - **Bundler:** the single root `awm/vite.config.ts` carries a `resolve.alias` that maps `@awm/<name>/<sub>` → `ui_components/<name>/src/<sub>` (subpath rule first) and `@awm/<name>` → `ui_components/<name>/src/index.ts`. This is the bundler analogue of the Python dist-root glob (`from awm.x import …`).
  - **Typechecker/IDE:** `awm/tsconfig.json` `paths` carry the same `@awm/*` glob, so `tsc`/`svelte-check` follow cross-component imports without a build.
- **Tree-shaking parity.** Vite only honors package `sideEffects` under `node_modules`; aliased first-party source is otherwise assumed side-effectful, which would bundle every barrel re-export. The root config's `build.rollupOptions.treeshake.moduleSideEffects` marks component `.ts`/`.svelte` source side-effect-free so a page bundles **only** the components it imports — while query-bearing virtual CSS modules and real `.css` keep their side effects, so a used component's compiled `<style>` and explicit `import '@awm/primitives/style.css'` always survive. (Don't drop this without re-diffing a built `dist/` against a known-good build; it's the seam that keeps unused-component CSS out.)
- **Third-party deps** are declared once, centrally, in the root `awm/package.json` (svelte, vite, the svelte plugin, `bits-ui`, etc.) — the Python model. A component needing a new third-party dep adds it to the root, never to a resurrected per-component manifest.

### Serve contract

A page's serve contract is purely a built `awm/pages/<name>/dist/` directory (+ optional sibling `prefix.txt`). Nothing in the serve path reads any frontend source or config:

- **Prod** registers a `kind=page` base via `POST /hub/page/register` with a dir path (`registry.register_page`); the gateway serves it via StaticFiles at `/ui/<name>`.
- **Shadow** registers via `_shadow_page_target` reading `pages/<name>/dist` + `_read_prefix_txt`.

Because both consume only `dist/`, the build invocation and authoring file set can change freely without touching the gateway.

### Operating SOP — build & shadow a page

```bash
# Build (from awm/): once per machine, then on every source change.
cd /home/tony/agentic_workspace/projects/awm/<scope>/awm   # your scope, NOT projects/awm/dev
npm install          # once — installs the central third-party deps
npm run build        # runs scripts/build.sh: each page → awm/pages/<name>/dist/

# Shadow against the RUNNING dev sandbox (:7821), from your scope worktree.
cd /home/tony/agentic_workspace/projects/awm/<scope>
awm dev shadow --port 7821 pages/<name>     # overlays dist/ at /ui/<name> on :7821
# Visit http://127.0.0.1:7821/ui/<name>/ — Ctrl-C pops the overlay; dev's base resumes.
```

`npm run build` builds every `pages/*/` that has an `index.html` (source-only placeholders are skipped) with the one root config. Shadow reads the built `dist/` + `prefix.txt`, so **build first**. Only the `dev` scope runs the sandbox (`awm dev start`); every other scope shadows the already-running hub — don't start a second one (see README § *Iterating on a page in a scope*).

## Federation

**`FEDERATION.md` is the reference** — read it before touching anything cross-node. The one-line shape: the loopback gateway stays open and unauthenticated, and a peer's services are reached **directly on that peer's `httpsfront` edge** over CA-verified TLS with a bearer fetched by ssh — never relayed through a gateway, never replicating a database. The gateway is a *directory* (`peer_resolve`, `peer_providers`), not a router: a call that belongs to a peer comes back as that peer's address for the caller to dial, never forwarded.

Two things bite agents who assume otherwise. First, this is **not** the retired v0 federation (cr-sqlite replication, leader election, a `peers`/`peer_sync_state` registry) — that really is gone, and git history for the deletion is not a guide to the current system. Second, a **singleton is re-homed per node**, not per call: `AWM_TWOFA_PEER` / `AWM_SOCIAL_PEER` / `AWM_SSH_SLOT_PEER` in `<workspace>/.awm/env` name the node that owns each singleton, and `gatewayclient.call_maybe_peer` / `call_sync_maybe_peer` / `subscribe_maybe_peer` are the single branch point so a consumer can never half-route. Use them even from sync code; a hand-rolled local POST is how one consumer ends up borrowing while another does not.

**What is singular is the resource, not always the service.** `2fa` is singular whole: a borrowing node may still run it for its local verbs, but must not act as owner — its `/approve` listener defaults off wherever `AWM_TWOFA_PEER` is set, because Duo's attempt budget is per account and two listeners spend it twice. `social` is the opposite shape: every node wants its own Slack, Gmail and buckets, and only the *identity* is singular. Mark such an account `singleton = true` in `social.toml`; the owner (selector unset) connects it, a borrower connects it not at all and forwards verbs naming it. Getting this wrong put two bots on one Discord token, and it surfaced as a flaky slash command rather than a config error — Discord hands an interaction to one session and the loser answers `10062 Unknown interaction`.

## Implementation file map

The Service Hub section above carries the *external* contract; this maps each piece to the file that implements it if you're about to change it (modular tree under `awm/gateway/awm/gateway/`). When you need to locate something *not* mapped here — a symbol, its callers, the blast radius of a change — query the **`graphify` MCP tool** (`find`/`refs`/`query`/`affected`) before spawning an Explore agent; it's an AST graph of this tree (it indexes the deployed/release tree, so it lags uncommitted worktree edits). The map:

- **Service discovery** — `hub/discovery.py` (filesystem scan of `awm/services/*` for `run.sh`; reads `.awm/services/enabled.json`).
- **Registry overlay + kinds** — `hub/registry.py` (one `_stacks` dict per prefix; base + at most one live overlay via `replace_overlays` = last-connect-wins eviction, NOT LIFO stacking; `kind` Literal = `url` | `static` | `page` | `service`; `register_page`).
- **RPC layer** — `hub/rpc.py` (in-memory `ControlChannel` per service, `_pending` call table, subscriber registry, session table, bridge id allocator).
- **Service translator + bridge** — `hub/proxy.py::proxy_service_http` / `open_session_via_http` / `proxy_session_ws` / `proxy_service_emit_ws`.
- **Supervisor + PID journal + bootstrap** — `hub/supervisor.py::reconcile_journaled_services` / `bootstrap` / `spawn_service` / `kill_pid_group` / `supervise_disconnect`; state at `<AWM_DIR>/state/services.json`. Injects only the three env vars and runs `bash run.sh`.
- **Catalog (manifest → MCP/CLI/HTTP)** — `catalog.py` (`_tool_name`, `list_tools`/`dispatch` for the expanded surface; `list_domain_tools`/`_describe_domain`/`_dispatch_domain` for the collapsed `?view=domains` surface; `/tools`/`/invoke`).
- **Federation directory** — `peers.py` (name → edge address) + `peer_catalog.py` (name → domains it provides, plus the default-provider rules and the `PeerRedirect` dispatch raises instead of relaying). `peer_files.py` is the client-side other half: after a proxy follows a redirect, it turns any `files[]` the peer returned into local copies (`gatewayclient.fetch_peer_file` over the peer's `/files` mount). It lives in its own module because **both** `mcp_stdio.py` and `mcp_server_sdk.py` must call it — inlining it in the default proxy would make the documented rollback silently hand back paths that exist only on the peer.
- **Gateway control ops** — `gateway_ops.py` (`GATEWAY_OPERATIONS`); generators in `operations.py`.
- **CLI** — the `gateway` + `services` groups are **generated** from `GATEWAY_OPERATIONS` by `register_cli_commands`, with a few hand-authored commands attached; `awm dev shadow` (search `dev_app`) and the page-shadow helpers (`_shadow_page_target`, `_read_prefix_txt`, `_post_page_register`) live in `cli.py`.
- **Frontend** — one root `awm/vite.config.ts` (the `@awm/*` alias + tree-shake rule), one `awm/scripts/build.sh` (the per-page build loop), `awm/package.json` (central third-party deps + `npm run build`). Components at `awm/ui_components/<name>/`, pages at `awm/pages/<name>/`.

> The `awm/gateway/awm/gateway/` nesting is **intentional** — a PEP 420 namespace layout so multiple uninstalled worktrees can each shadow `awm.gateway` via `PYTHONPATH` without colliding. It is not a stutter to "fix."

## Awm editable install gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `…/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds: run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping; spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree; or merge dev → release to advance the mapping's target (what the deploy step does). See memory `[[awm_two_source_trees]]`.

## Running tests

Tests are **per-dist**: each dist (gateway + the five feature services) owns its own `tests/` directory and `pyproject.toml` (carrying that dist's pytest config — `asyncio_mode`, markers). The dists all merge into the PEP 420 `awm` namespace, so a single `pytest` over the whole tree can't import every dist at once (two source roots both providing `awm` shadow each other). The runner invokes pytest **once per dist** with that dist's source root + the shared components on `PYTHONPATH`:

```bash
# Full suite — every dist, each with the correct PYTHONPATH. Run before merging.
awm/gateway/scripts/run-tests.sh

# Only the named dists:
awm/gateway/scripts/run-tests.sh scopes gateway

# Pass extra args through to pytest:
PYTEST_ARGS="-x -q" awm/gateway/scripts/run-tests.sh
```

The script reports pass/fail per dist and exits non-zero if any failed. Cross-dist imports inside a test must be **lazy** (inside a fixture/function), never at module top level — a top-level cross-dist import re-triggers the namespace-shadowing problem the per-dist runner exists to avoid.

A dist whose tests exist but which is missing from the runner's `DISTS` map is reported as `unknown dist` and silently never runs — `reflection` sat that way for months. When adding a service with tests, add it to both `DISTS` and `ORDER`.

## Agent rules

1. **The native `debrief` skill (`~/.claude/skills/debrief/`) is mandatory at end-of-session** — it keeps `.awm/history.md` accurate across all scopes.
2. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.

## Workspace Layout

| Path | Purpose |
|------|---------|
| `WORKSPACE.md` | Scope-agent orientation — injected into every scope agent's context |
| `AGENTS.md` | This file: awm internals and workspace operation |
| `README.md` | Human setup/usage guide (never auto-injected) |
| `awm/` | AWM service package (Python) |
| `skills/` | Reference protocol docs (read-only; the skills *service* is retired) |
| `data/` | Shared data (per-project; raw, staged, outputs) |
| `projects/` | Project bare repos + git worktrees (scope agents work here) |
| `tasks/` | Per-task workspace units — DAG node execution sandboxes (gitignored) |
| `.awm/` | Workspace runtime state |
| `.mcp.json` | Canonical MCP server registry — fans out via the exporter framework |

## Finding Projects

**Deliberately not enumerated.** Projects and their scopes are created, renamed and completed constantly; any list written here is stale within days. Enumerate live instead:

- `project(verb="search")` — every project; `awm project search [query]` from a shell.
- `scope(verb="search", args={project:<p>})` — a project's scopes, branches and worktrees.
- `ls projects/` — the on-disk truth.

What a given project is *for* lives in that project's own worktree, not here.

## Data

**Data is versioned by the same commit that versions the code.** A DVC-backed project keeps its data at `<scope>/data/<chunk>` and a tracked pin beside it at `<scope>/data/<chunk>.dvc`. The bytes live once, in a content-addressed cache shared by every scope and project on the machine.

There is one lever. A commit records your code and the exact data it was built against, together; merging a branch brings its data with it. So:

- **To save data you wrote:** `dvc add data/<chunk>`, then commit the changed `.dvc` pin alongside your code. There is no data verb, no data branch, no promote.
- **To take a sibling's data:** merge their branch. A post-merge hook checks the files out.
- **Isolation is branch isolation.** Your pins are yours until someone merges them.

The verbs are on the `scope` and `dvc` domains — `describe` them. Two mechanical facts, and only these two:

1. **Materialised files are read-only hardlinks into the shared cache.** Editing one in place corrupts that object for every other scope and every historical commit that pins it. Write a new file, or `dvc unprotect <path>` first.
2. **Never run a bare `dvc gc`.** It collects against one worktree's view of a cache the whole workspace shares. Use `data_gc`, which makes you name what to keep.

Projects that have not been converted keep a plain `.awm/data` symlink to `data/{project}/` — shared, unversioned. Nothing migrates them; a project gets wired the first time a scope is created or healed after its checkout carries a tracked `.dvc/config`.

**Delete superseded data.** That is what versioning buys: an old version stays reachable from the commit that pinned it, so you never need two live copies to answer "which one is current?"

### Off-site backup

Two nightly jobs leave this machine, and they are deliberately different things. Both are scheduled inside the dvc service.

- **The archive** pushes the DVC cache to chinook, **append-only**. Nothing ever deletes there, which is what makes a local `dvc gc` recoverable.
- **The mirror** copies the rest of the workspace to a sibling remote root, and it **deletes**: a file removed here is removed there on the next run. It skips the cache and the materialised checkouts — those are hardlinks the archive already holds, and Globus cannot preserve a hardlink.

The two never touch each other's bytes, and that is structural rather than a rule: every mirror destination path sits under `…/workspace/`, so a delete-enabled transfer cannot reach the archive whatever its exclusion logic does. Neither is a substitute for pushing a branch — `dvc(verb="coverage")` reports what exists on no remote.

Two things a restore will not hand back: symlinks are not followed and not recreated, and a directory deleted at the workspace *root* is covered by no transfer item, so it lingers remotely rather than being pruned.

## Skills

The end-of-session **debrief** is a native Claude Code skill (`~/.claude/skills/debrief/`). Other procedural references live on disk under `.awm/skills/` and are Read-able when relevant; the skills *service* is retired, so they are reference-only rather than searchable.

Session execution traces go in the journal via `scope_post` (kind=journal).

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: the agent reads `.awm/context.md` and runs the Startup Ritual in `WORKSPACE.md`.
3. **Work**: code in the worktree, data at `data/`.
4. **Debrief**: the agent runs the native `debrief` skill.
5. **Complete**: `scope_complete` updates status, optionally merges the branch.

**Per-user data is a scope too.** The public app keeps each signed-in person in project `userdata`, scope `<name>`, branch `user/<name>`. A service that partitions by user resolves the caller through `awm.config.userroot` (`resolve(as_)` → the user or `None`, `root_for(user)` → that worktree, `state_dir(service, user)` → its own index) and commits its subdirectory with `awm.config.autocommit`. `userroot.wrap_handlers` binds the caller for every verb. Notes and drawio are the reference implementations; a new per-user service (Logseq) reuses the same three calls and never chooses a path itself.

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | Cross-cutting work on a single shared frontend component. |
| `svc-*`  | service   | Cross-cutting work on a single long-running backend service. |
| `feat-*` | feature   | Multi-package composition that wires components, services and pages together. |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume. |

Scopes predating the convention keep their flat keyword names.

**Nested names.** A scope name may contain `/`, which is worth it when one project holds several products and a flat list stops saying which is which. Three consequences:

- The **branch is named after the scope**, not `feat/<scope>` — pass `branch_name` at create time.
- **Git stores refs as paths**, so a nested branch permanently forbids a bare branch of its first segment, and vice versa. `scope_create` refuses the collision by name.
- **A project name never nests.** A slashed project would put a second `.bare` one level down.

References stay `project/scope` and split on the *first* slash.

**Composition scopes.** A `feat-*` scope may be a *standing* composition scope owning the cross-service wiring for one feature family, running its own isolated dev sandbox on a port pinned in a gitignored `awm/gateway/dev/.env`. `dev` is not a feature scope — it is the release-staging worktree.

### Hubs & peripherals (scatter / gather)

A **hub** scope integrates work from a set of **peripheral** feature scopes via two batch git operations — **gather** (merge each peripheral into the hub branch) and **scatter** (merge the hub branch back out). Both are local-only and **stateless**: the peripheral list is passed explicitly, so this table *is* the convention they read from. Drive them with the `scatter-gather` skill or `scope(verb="gather"|"scatter", …)`.

| Hub | Branch | Peripherals (seed — edit as the family changes) |
|-----|--------|-------------------------------------------------|
| `feat-dag` | `feat/feat-dag` | `svc-agents`, `svc-orchestrator`, `svc-events`, `web-stt`, `web-tts`, `web-ui` |
| `feat-gamebot` | `feat/feat-gamebot` | `svc-effector`, `svc-events`, `rlm-browser`, `rlm-factorio` |
| `feat-fleet` | `feat/feat-fleet` | `svc-agents` |
| `dev` | `dev` | all promotable scopes |

Each hub may mirror its own row into its `.awm/context.md` for a hub agent to find without walking up here.

## Dev protocol — parallel consumer/library scopes

A project that **consumes a shared-library project as a git submodule** may need to work the library and the consumer in lockstep across several scopes at once. If every consumer scope pins the submodule to the same library branch, parallel library edits collide on that one branch. This protocol gives each consumer scope its own library branch and worktree. It is a template: a second consumer adopts it by filling slots, and the live instantiation belongs in that consumer's own repository, not here.

| Role | consumer scope | ↔ library scope |
|------|----------------|-----------------|
| lead / integrator | `dev` | `<consumer>` (hub) |
| parallel worker 1..N | `devN` | `<consumer>N` |

`dev`↔`<consumer>` are the two hubs; `devN`↔`<consumer>N` are the peripherals — the same hub/peripheral shape as above, one pairing per project side.

**Submodule tracking is push-free and local.** Each consumer scope's `src/<lib>` submodule carries an `awm` remote pointing at the library project's local `.bare` (`origin` stays the GitHub url so `clone --recurse-submodules` still works), is checked out on its paired branch with upstream set to `awm/<branch>`, and names that same branch in `.gitmodules`. The library worktrees and the consumer submodule checkouts therefore share one local bare repo. Preferred workflow: edit in the library worktree, then `git -C src/<lib> fetch awm` in the consumer and bump the gitlink.

**Promoting a worker is a parallel merge** — one gather per side, then bump the consumer hub's gitlink to the new library-hub tip.

Two things bite here specifically: `git submodule update --remote` follows `.gitmodules` on the **default** remote (GitHub), not `awm`, so sync with explicit `fetch awm` / `push awm`. And `git worktree move` **refuses on a worktree containing submodules** — move the directory by hand, then `git worktree repair`, rename the `.bare/worktrees/<name>` admin dir to match, and fix each submodule's gitdir pointer and `core.worktree`.

## CLI Quick Reference

`awm <command> --help` for full options. **The CLI mirrors the full expanded surface**: beyond gateway control, it generates one `awm <domain> <verb>` command per registered service tool from the same live catalog the MCP surface reads, so the two never drift. `awm <domain> --help` lists a domain's verbs; `awm <domain> <verb> --help` shows that tool's exact parameters from its `inputSchema`.

| Command | Purpose |
|---|---|
| `awm gateway init / status / serve / stop / restart` | Core lifecycle |
| `awm project create <name>` | Create a project |
| `awm scope create / list / complete` | Scope worktree management |
| `awm scope heal [--dry-run]` | Idempotent repair pass |
| `awm scope data-status / data-mount / data-gc` | A scope's data view; what materialises; reclaiming cache |
| `awm dvc sync / pull / coverage` | Off-site: push the cache, restore one scope, audit what is uncovered |
| `awm gateway register / list / deregister` | Service Hub control plane |

## What goes in this file

AGENTS.md holds two things: the awm-internal architecture for agents modifying awm itself — gateway, registry, supervisor, RPC envelope, operations/catalog generation, service lifecycle, frontend component system — and the procedures for operating the workspace: layout, data, backups, scope lifecycle and naming, hub integration, the CLI surface.

Scope-agent orientation goes in `WORKSPACE.md`, which is injected everywhere and pays for every line. Human install/usage goes in `README.md`. What a project is for goes in that project's own files.

Nothing enumerable goes here either, with one standing exception: a table that *is* a decision this file makes — the scope-prefix families, the hub/peripheral roster — rather than a snapshot of state it reports.
