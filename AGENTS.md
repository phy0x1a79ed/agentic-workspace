# AWM Internal Architecture

*Internal architecture reference for agents working ON awm itself — the gateway, the registry/supervisor, the RPC envelope layer, the operations/catalog generation layer, the feature-service contract, and the frontend component system. Auto-injected only when the agent's cwd contains this file at its root: `projects/awm/*` scopes inherit it via `.bare`-worktree sharing; other projects' agents never see it.*

For workspace structure (paths, MCP tools, project map, scope lifecycle) see `WORKSPACE.md` (auto-injected before this file). This file assumes you're modifying awm itself.

## Architecture overview

awm is a modular **gateway** plus a set of out-of-process **feature services**. The gateway (`awm/gateway/`, package `awm.gateway`) is the sole interface (CLI, HTTP, MCP stdio) and the coordination hub; it owns no tables of its own and boots standalone. Feature surfaces appear as their modules register.

The tree is a nest of pip dists under `awm/`, all merging into the PEP 420 `awm` namespace:

- **Gateway** — `awm.gateway`: the interface + hub. Discovers, bootstraps, and supervises services; renders their APIs onto MCP/HTTP/CLI.
- **Shared Python components** (`awm/service_components/`, pure imported source, no `install.sh`) — `awm.config`, `awm.persistence`, `awm.gatewayclient`, `awm.agentcore`.
- **Frontend source** (`awm/ui_components/` + `awm/pages/`) — shared Svelte components imported by name (`@awm/<name>`) and the static page bundles that compose them (see § *Frontend component system*).
- **Feature services** (`awm/services/<name>/`, each its own dist + DB + `run.sh`) — `scopes`, `agents`, `artifacts`, `skills`, `discord`, plus the voice pair `stt`/`tts`.

Each feature service owns its own SQLite DB; there is no shared `state.db`. Cross-service references are **natural keys** (an agent = its `(project, scope)` pair) validated by calling the owning service over gateway RPC, never by importing it. `scopes` owns identity and exposes it via the RPCs frozen in `awm/services/scopes/IDENTITY_CONTRACT.md`.

**`agentcore` — the pluggable harness layer.** `awm.agentcore` (a leaf imported-source component — zero `awm.config`/gateway imports) owns "talk to a CLI agent harness and yield normalized events," and nothing about scopes or the gateway. `open_agent(AgentConfig{harness:'claude'|'opencode', mode:'live'|'oneshot', model?, params?, permissions='full', workdir?, resume_id?, mcp_config?, system_prompt?}) -> AgentSession` (`subscribe()`/`send()`/`close()`); `run_once(config, prompt, schema?)` is the one-shot wrapper. Both harnesses are **subprocess** drivers — claude (`--print` stream-json, `--permission-mode=bypassPermissions`) and opencode (warm `opencode serve`, `--dangerously-skip-permissions`), no Agent SDK, no OpenRouter — mapped into one `AgentEvent{id, kind:'message'|'partial'|'tool_use'|'tool_result'|'status'|'result'|'error', text?, data?, ts}`; `id` is the dedupe/cursor key end-to-end. The agents service builds its sessions through it; `stt`'s convo-cleanup uses `run_once(harness='opencode', mode='oneshot')`.

**A scope IS the channel.** There are no `rooms`/`messages`/`session_logs` tables. One `scope_posts` table (kind = `message`/`journal`/`system`/…) + `scope_subscribers`, addressed by `(project, scope)`, carries everything; non-agent inboxes (`user:`/`project:`/`workspace`) are non-literal channels (`owner_project=''`). The surface is `scope_post` / `scope_fetch` / `scope_subscribe` / `scope_unsubscribe` (in `awm/services/scopes/awm/scopes/channel.py` + `operations/scope_channel.py`). The chat data-flow is **asymmetric**: the frontend POSTs human messages to the scope (`scope_post`) and SUBSCRIBES to the agents service for output; the agents service subscribes to its scope's channel to feed the agent's stdin and records the agent's per-turn acts in `agent_transcript` (exposed live over the agents `transcript` direct WS — backfill-from-cursor + live push, de-duped by act `id`), **not** auto-posted back to the scope. Only deliberate agent messages (e.g. a debrief) are an explicit `scope_post`.

## Service Hub

`awm.gateway.server:app` is the gateway — the sole interface + coordination hub, owning no tables of its own. A few path prefixes are *registered* at runtime; matched requests dispatch to one of four kinds:

| Kind | Surface | Typical caller |
|---|---|---|
| `service` | RPC-over-WS at `/svc/<name>` | a folder under `awm/services/<name>/` discovered + run by the gateway |
| `page` | static bundle at `/ui/<name>` | a built bundle under `awm/pages/<name>/dist/` |
| `url` | HTTP/WS proxy at any prefix | external services registered via `awm gateway register --url ...` |
| `static` | static bundle at any prefix | external bundles registered via `awm gateway register --dir ...` |

Each prefix maps to a stack of records (base + optional overlays). Overlays are pushed by `awm dev shadow`; the topmost overlay receives traffic, with base traffic resuming instantly when an overlay's lease closes.

### Feature services — the `awm/services/<name>/` contract

The first-class way to add a backend is a folder under `awm/services/<name>/` with a self-contained executable `run.sh`. The gateway **discovers** it by filesystem scan (`awm/gateway/awm/gateway/hub/discovery.py`), **bootstraps** it on first boot, respawns it durably, and injects **exactly three** env vars (`AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_SERVICE_ID`) — no auth, ever. Enable/disable state lives in `.awm/services/enabled.json` (a service absent from the file is enabled); the operator surface is `awm services list|start|stop|restart|enable|disable|reap [name|--all]`. The full human-facing authoring contract is **README § *Authoring a service***.

Foundation facts for anyone touching this:

- **Per-service SQLite DBs, no shared `state.db`.** `awm.persistence.databases` is the factory — `get_connection(service)` + `init_service_db(service, schema_sql, schema_version=…)` give each service its own DB at `AWM_DIR/services/<svc>/<svc>.db`. Raw SQL lives behind `awm.persistence.dao.BaseDAO` subclasses shipped per-service; each service self-seeds via its own `seed.py`. Per-service schema + legacy seed shapes are documented in `awm/service_components/persistence/SCHEMA_HANDOFF.md`.
- **Services are OUT-OF-PROC**, spawned + PID-journaled by the supervisor; the control-WS lease is liveness. Each service is a self-contained `run.sh` (the only thing the gateway runs — `bash run.sh`) plus a tiny `hub_adapter.py` built on **`awm.gatewayclient.ServiceAdapter`** (the reusable register→ready→serve→dispatch→reconnect base). `awm.gatewayclient` also carries `call`/`call_sync`/`RefCache` for the service→service direction. The `/svc/*` control plane is unauthenticated (loopback only). `awm/services/discord/` is the worked example to copy.
- **Install/iterate each module via its own `install.sh`, never hand-rolled `pip`.** `awm/gateway/install.sh` is the composition root — it installs `config` + `persistence` and every feature dist `--no-deps`, then the gateway itself (resolving third-party deps). Imported-source components (`config`, `persistence`, `gatewayclient`, `agentcore`, `ui_components`) have no `install.sh`. Override the target env with `AWM_ENV`.
- **Registration contract.** A service declares its API as a serializable `ready.api` manifest (`functions`/`emitters`/`subscriptions`); that threads into the MCP/CLI/HTTP generation layer (manifest → `Operation` → the `operations.py` compiler; dispatch is catalog-owned). The hub-mediated service↔service comms model — `call` request/reply, `emit`/`sub` pub/sub, `Bridge` streaming, all identity-aware via `as_`, never direct sockets — is documented in `awm/gateway/awm/gateway/catalog.py`. The catalog is the single live source the gateway renders to all three surfaces; `/tools` and `/invoke` read from it. A manifest function may carry an optional `"tool"` key to set its exact MCP-surface name, decoupling the projected label from the internal op `name` used for RPC dispatch — so the surface reads as clean `<domain>_<verb>` (`scope_create`, `agent_spawn`, `scope_resolve`) while internal names (incl. the frozen `IDENTITY_CONTRACT` camelCase RPCs) are unchanged. `_tool_name` honors it; `list_tools` warn-and-skips duplicate projected names (override names must be globally unique).
- **The gateway's own control plane runs through the same compiler.** Its status/restart/mcp-sync, hub list/deregister, and services-lifecycle ops are declared once as `GATEWAY_OPERATIONS` (a list of `operations.Operation` in `awm/gateway/awm/gateway/gateway_ops.py`); the three generators in `operations.py` (`operations_to_mcp_tools`, `register_fastapi_routes`, `register_cli_commands`) project them onto MCP (via `catalog.list_tools`/`dispatch`), HTTP (`server.py` startup), and CLI (`cli.py`). A new gateway control op is one `Operation`, never a hand-rolled CLI command + HTTP route + native MCP tool in three places.
- **Concurrency.** One server loop owns all async hub state; blocking work is offloaded (`run_in_threadpool` / `run_in_executor`); `asyncio.run()` is banned inside the daemon (survives only in the CLI/MCP-proxy/entrypoint processes). `catalog.dispatch` is async, `/tools` is sync.

The `url` / `static` kinds and the `awm gateway register --url|--dir` lease commands remain only for **external** bundles/services (a remote HTTP/WS upstream, or a hand-built static dir) — not for awm's own backends.

### Service lifecycle

The gateway↔service lifecycle covers all start/stop/crash directions deterministically; editing a backend never leaks orphans.

- **First-boot bootstrap (reconcile-then-bootstrap).** Boot doesn't depend on a populated journal. The gateway (1) `reconcile_journaled_services()` for anything already journaled, then (2) **bootstraps** any discovered-but-unjournaled enabled service — so a fresh clone or a wiped `.awm/state/services.json` comes up with every enabled service running. Discovery is a filesystem scan of `awm/services/*` for `run.sh`; no hardcoded name list, no manifest-sync step. A disabled service (per `.awm/services/enabled.json`) stays down across restarts.
- **No dev auto-reload.** `awm/gateway/dev/run.sh` launches uvicorn **without** `--reload`/`--reload-dir`, so saving a backend file never swaps the worker out from under its spawned services. Backend edits require an explicit `awm dev restart`.
- **Service-side give-up (`gatewayclient/adapter.py`).** `ServiceAdapter.run()` exits 0 (no retry) on a stand-down signal — a `409` register, a control-WS close `4409` (lease held) / `4404` (unknown), an upgrade `409`/**403** (the gateway closes an unknown `service_id` *pre-`accept()`* with `close(4404)`, which the `websockets` client surfaces as HTTP **403** on the upgrade), or an in-band `{"kind":"shutdown"}` frame — via a `GiveUp` sentinel. It also exits once the gateway has been unreachable past `AWM_RECONNECT_DEADLINE_S` (default 10s, env-tunable), measured from the last **confirmed-up** disconnect (a transient blip still retries with backoff). The per-loop `last_up` deadline clock is refreshed only on a confirmed inbound frame, so an accept-then-immediately-close can't keep resetting it. A **self-minted** sid (empty `AWM_SERVICE_ID`) is cleared on every disconnect so the next loop re-registers and deterministically hits `409 → GiveUp` against a live incumbent; a hub-**assigned** sid (respawn-by-sid) is kept. This is the backstop for a hard-killed gateway, where lifespan shutdown never runs.
- **Duplicate rejection (`api/hub.py::service_register`).** A second instance registering under a name whose record still holds a **live** control-WS lease gets `409` and stands down; the incumbent is untouched. Takeover of a **dead** (lease-not-held) record still replaces in place. Respawn-by-sid skips `_register` entirely.
- **Graceful teardown (`server.py`).** On SIGTERM/SIGINT the gateway drains its services **in-band**, then force-kills any straggler, then clears the journal so the next boot bootstraps clean. The drain is one reusable `server._drain_services()` coroutine. In-band delivery works via a **loop-level signal override** installed at lifespan startup that takes ownership of SIGTERM/SIGINT *before* uvicorn's own handler, so the gateway delivers the `{"kind":"shutdown"}` frame over each live control WS **while it is still open** — each service stands itself down, drops its lease, and exits. The override captures uvicorn's `Server` off `signal.getsignal(sig).__self__` — **this uvicorn (0.x) registers `signal.signal(sig, server.handle_exit)`, NOT `loop.add_signal_handler`**, so a `loop._signal_handlers` capture finds nothing; `getsignal` is the correct seam — then installs `loop.add_signal_handler(sig, _on_signal)` (works on uvloop too) which drains then flips `server.should_exit`. A second signal force-exits (operator escape hatch). If the Server can't be captured (TestClient / non-main-thread) it falls back to a flag-only wrapper and the drain runs from the lifespan-shutdown backstop. **Force-kill is the backstop, not the primary mechanism** — it fires only for a straggler whose lease is still held after the ~8s grace window, or whose process is alive while its lease is already gone.
- **Crash-respawn watchdog (`supervisor.supervise_disconnect` + `hub.py` disconnect hook).** An unexpected control-WS disconnect schedules a watchdog: re-register the record (so a quick self-reconnect is accepted), wait `_RECONNECT_WINDOW_S` (10s), respawn from the journal if still silent. Gated on not-shutting-down + journal-entry-present + enabled + not-reconnected. `awm services stop` drops the journal entry **before** killing, so a deliberate stop is never respawned. Two independent 10s windows exist — the service's give-up deadline and the gateway's reconnect/respawn window (service-side vs gateway-side authority).
- **Orphan reaper backstop (`gateway_ops.py`).** `awm services reap` scans `/proc` for `awm.<svc>.hub_adapter` processes whose `AWM_HUB_URL` origin (`host:port`) matches this gateway and which hold **no** live registry lease, then SIGTERM→SIGKILL via `supervisor.kill_pid_group`. `--dry-run` lists only. Origin-keyed, so a prod reap (`:7819`) never touches the dev hub's children (`:7821`); never reaps a live-lease holder or the gateway's own pid.

dev caveat: `dev/run.sh` records the **mamba-run wrapper** PID in `dev.pid`, so a bare `kill -TERM $(cat dev.pid)` hits the wrapper, not uvicorn — `awm dev stop` (which `pkill -P`s the uvicorn child) and prod `systemctl stop` (systemd signals the `awm gateway serve` process directly) both deliver the clean signal that triggers the in-band path.

### Hub origin = gateway port

There's only ever one hub origin per node — the gateway process. Which port depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd-managed) | `7819` | `awm.service` on the host |
| Dev sandbox: `projects/awm/dev/` | `7821` | `awm dev start` |
| Dev sandbox: `projects/awm/web-ui/` | `7831` | same |
| Dev sandbox: `projects/awm/web-backend/` | `7841` | same |
| Dev sandbox: any other scope (fallback) | `7851` | same |

Per-scope port bands are derived from the worktree dirname, so dev sandboxes run side-by-side with prod (and each other). **Substitute your sandbox port** whenever a section says `:7819`. The CLI's own target hub is `BASE_URL`, computed from `AWM_PORT` (default `7819` = prod) — `AWM_HUB_URL` is injected into *services*, not consulted by the CLI. `awm dev shadow` takes a `--port` (default `7821`) to pick which hub it shadows onto, so you can't fat-finger a shadow onto prod.

### External registrations (`kind=url` / `kind=static`)

For an **external** upstream the gateway should front, register a lease via `awm gateway register`. These POST/DELETE/WS to `/hub/*` on the gateway origin:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/hub/register` | Register a service; returns `service_id` + `lease_ws_path` |
| `WS`     | `/hub/lease/{service_id}` | Hold lease; disconnect → eviction |
| `GET`    | `/hub/services` | List registrations + lease state |
| `DELETE` | `/hub/services/{name}` | Force-evict by name |

All unauthenticated — the gateway binds loopback only. `kind=static` serves canonical paths only (a file at the exact path, or a directory's `index.html`; a miss is a 404 — no `Accept` fallback, no SPA shell synthesis). For deep-link refresh in a SvelteKit/React-Router bundle, prerender every route; for routes the server can't enumerate, front the upstream as `kind=url`.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix. `/hub` and `/hub/*` are reserved.
- **`AWM_WORKSPACE` + `AWM_HUB_URL` attach to a sandbox, not prod.** Without them, the CLI hits global discovery and may target prod `:7819`. The dev starter exports both for its children; if you shell out separately, export them yourself. To check which hub a running process was set up against: `tr '\0' '\n' </proc/<pid>/environ | grep -E 'AWM_WORKSPACE|AWM_HUB_URL'`.
- **Never run two gateways on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are how dev parallelism works.

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

## Federation: retired

Federation is gone — see git history for the deletion. No `/peer/*` routes, no `awm peer *` CLI, no `awm-exposed.service`, no auth layer, no `peers`/`peer_sync_state` tables. The local listener (`awm.service` on `127.0.0.1:7819`) is the only listener. Single host, no auth, plain HTTP loopback.

## Implementation file map

The Service Hub section above carries the *external* contract; this maps each piece to the file that implements it if you're about to change it (modular tree under `awm/gateway/awm/gateway/`):

- **Service discovery** — `hub/discovery.py` (filesystem scan of `awm/services/*` for `run.sh`; reads `.awm/services/enabled.json`).
- **Registry overlay + kinds** — `hub/registry.py` (one `_stacks` dict per prefix; base + overlays LIFO; `kind` Literal = `url` | `static` | `page` | `service`; `register_page`).
- **RPC layer** — `hub/rpc.py` (in-memory `ControlChannel` per service, `_pending` call table, subscriber registry, session table, bridge id allocator).
- **Service translator + bridge** — `hub/proxy.py::proxy_service_http` / `open_session_via_http` / `proxy_session_ws` / `proxy_service_emit_ws`.
- **Supervisor + PID journal + bootstrap** — `hub/supervisor.py::reconcile_journaled_services` / `bootstrap` / `spawn_service` / `kill_pid_group` / `supervise_disconnect`; state at `<AWM_DIR>/state/services.json`. Injects only the three env vars and runs `bash run.sh`.
- **Catalog (manifest → MCP/CLI/HTTP)** — `catalog.py` (`_tool_name`, `dispatch`, `/tools`/`/invoke`).
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

The script reports pass/fail per dist and exits non-zero if any failed. Known dists: `gateway scopes agents artifacts skills discord`. Cross-dist imports inside a test must be **lazy** (inside a fixture/function), never at module top level — a top-level cross-dist import re-triggers the namespace-shadowing problem the per-dist runner exists to avoid.

## Agent rules

1. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
2. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.

## What goes in this file

AGENTS.md is the awm-internal architecture + implementation reference for agents modifying awm itself: how the gateway, registry, supervisor, RPC envelope layer, operations/catalog generation, service lifecycle, and frontend component system work, where each piece lives, and the operating SOPs for building on awm (service contract, page build/shadow). Workspace-structural orientation (paths, MCP catalog, scope lifecycle) goes in `WORKSPACE.md`; human install/usage goes in `README.md`.
