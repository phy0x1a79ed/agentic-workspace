# AWM Architecture

## Purpose & Contents

This file is the reference for agents that change awm itself. It covers the core binary and its gateway, plugins and the service contract, the catalog that renders plugins onto MCP, HTTP and the CLI, the timeout ladder, the verb gates, the isolated door, egress, usage per plugin, kept Claude Code sessions, the journal, the search archive, the service lifecycle, dev sandboxes, the data-layer internals, the frontend, federation pitfalls, a file map, and how to run tests.

Agent orientation goes in `AGENTS.md`. Workspace procedures go in `PROTOCOLS.md`. Human install and service authoring go in `README.md`. Cross-node design goes in `FEDERATION.md`.

Keep what the code cannot show: why a shape was chosen, which two things must agree, and what breaks silently when they stop agreeing. Point at the source for everything else.

## Overview

awm is one Rust binary, `awm`, plus a set of out-of-process **plugins**. The binary is the only gateway. It serves the CLI, HTTP and MCP, loads and supervises the plugins, and owns no tables. Feature surfaces appear as plugins register.

- **Core** — `core/crates/`, built into `~/.local/bin/awm`. `awm-gateway` is the gateway, `awm-cli` the command tree, `awm-mcp` the stdio MCP server, `awm-edge` the ssh transport between nodes, `awm-trust` the edge list, trust levels and the service door, and `awm-proto` the manifest and wire types.
- **Plugins** — each folder under `core/plugins/`, `awm/services/` and `awm/plugins/` with an `awm-plugin.toml`. A plugin under `core/plugins/` is a static Rust binary that the deploy places at `<AWM_DIR>/bin/<name>`. `awm services list` reports the live set. Each folder's `INSTALL.md` is its contract.
- **Shared Python components** — `awm/service_components/`: `awm.config`, `awm.persistence`, `awm.gatewayclient`, `awm.trustpolicy`, `awm.usage` and `grantlib`. They are libraries, not plugins. A plugin imports a library, never another plugin's code.
- **Frontend source** — `awm/ui_components/` and each plugin's `pages/`. See § *Frontend*.

### Ownership and identity

Each plugin owns its own SQLite DB. There is no shared `state.db`. Plugins refer to each other's records by natural key, such as an agent's `(project, scope)` pair. A plugin validates such a key by calling the owner through the gateway, never by importing it. `scopes` owns identity and exposes it through the RPCs frozen in `awm/services/scopes/IDENTITY_CONTRACT.md`.

### Harness plugins

A harness plugin integrates one coding-agent harness: `cch` for Claude Code, `dsh` for the DeepSeek harness. Each serves a hidden tool that implements `harness@1` (`docs/redux/drafts/harness-contract.md`). `reflection` is the one agent-facing tool. It holds what holds for every harness: who is calling, trust and mode per session, the R9 session id, the owed resumes and `wait`. A harness plugin finds its own sessions, types into them, starts, stops and resumes them, and reports events back to `reflection`. The gateway asks `reflection session_of` who owns a caller's pid, and `reflection` asks the harness plugins.

The harness plugins share the `awm-harness` crate (`core/crates/awm-harness`). Its `contract` module holds the harness@1 target parsing, result types and the `harness_event` client, which cch, dsh and reflection all use. Its `claude` module holds what Claude Code owns: the daemon roster, the job and session records, the launch, directory trust and typing into a session over the daemon's PTY socket. It reads no `AWM_*` variable and never names cch, and a layering test enforces this. cch's own policy, such as its lineage records and the restricted mode, lives in the cch crate.

### Kept sessions

A plugin keeps its state in SQLite, so the gateway's respawn brings it back whole. A Claude Code session keeps its state in its conversation, and only the daemon can bring that back. Three layers keep a session up:

1. `cch`'s keep unit holds the daemon in its own user unit, outside awm's control group and outside every terminal (`core/plugins/cch/src/keeper.rs`).
2. `cch`'s session layer ensures the daemon before a start, resume or stop. It refuses with `code: "daemon_down"` only when that fails. It revives pinned jobs once per boot.
3. `social`'s seat keeper holds the keeper policy: the restart record, the decision (wait, resume, start fresh, give up) and the breaker.

**CAUTION** A keeper must read `daemon_down` and `daemon.state` as "wait", never as "the session is gone". Stopping a pinned job runs `claude rm` and loses its conversation. That reading is what destroyed a seat on 2026-10-20.

The gateway's crash-loop breaker persists in `<AWM_DIR>/services.json`, because a gateway restart must not reset a crash-looping plugin's budget. `awm services start` is the operator's way back. A keeper's breaker persists in its plugin's DB for the same reason.

### The journal is the channel

The `kb` plugin serves the `journal` tool (`core/crates/awm-kb-journals`). Every post lives once, as one row of the `journals` LanceDB source, so a post and its search entry cannot disagree. `services/journal/journal.db` keeps the relational state: the tag registry and its clashes, edit history, inbox cursors, read receipts and session lineage. Its post tables are frozen. They are the pre-Rust record that `kb export-back` writes to on a rollback. The journal split out of `scopes` so that worktree lifecycle and messaging deploy and fail apart. A channel is addressed by `(project, scope)`. Tags from one fleet-wide vocabulary tell posts apart (`message`, `journal`, `brief`, `precedent`, …). A comment is a post with `reply_to`, kept in its thread's channel. A non-agent inbox (`user:`, `project:`, `workspace`) is a non-literal channel with `owner_project=''`.

`journal` validates a literal scope through the `resolveScope` RPC to `scopes`, and an unreachable `scopes` never blocks a post. The plugin publishes the `posts` topic with payload `{project, scope, post}`.

A scope reads its channel through a cursor in `scope_cursors`, one row per channel. The cursor belongs to the scope, not to a session, because seats churn and the scope persists. `journal inbox` lists the posts after it and `journal ack` moves it forward. A post the scope wrote itself never counts as unread.

**CAUTION** A literal scope name repeats across projects: most of them have a `dev`. `journal fetch` therefore refuses a `scope` with no `project`. Without that refusal the filter reads every project's channel of that name as one.

A post is passive mail. It starts no session and wakes no one. Agents message each other as `FEDERATION.md` § *Messaging* says.

A post that arrives through the edge takes its author from the gateway's `peer:<node>` stamp, never from the `author` argument. The claimed author moves to `meta.claimed_author`.

## The gateway

`awm gateway serve --state <AWM_DIR>` runs it, under the `awm` user unit (`core/deploy/awm.service`). It reads one file, `<AWM_DIR>/config.toml`, which `awm bootstrap` writes from an install config: the plugin roots, the profiles, the plugins to load or exclude, the `[env]` passed to every plugin, the `[[external]]` services and the listen addresses. `core/crates/awm-gateway/src/config.rs` is the schema.

**CAUTION** The gateway passes a plugin only `config.toml` `[env]`, never the old `<AWM_DIR>/env`. `awm deploy` names each key left only in the old file. Such a key reaches no plugin.

The routes are in `server.rs` `router()`. Their roles:

- `POST /invoke` runs one verb. `GET /tools` lists the catalog. `/mcp` is MCP over HTTP.
- `/hub/service/*` is the plugin control plane: register, the control WebSocket and the bridge.
- `/hub/emit/<service>/<topic>` and `/hub/session/<service>/<kind>` are the WebSocket streams. A session sends its `init` as the first frame. `?peer=<node>` opens either across an edge.
- `/s/<service>/…` proxies to a plugin's own loopback HTTP listener, its `mount`. A mount answers only while its service is ready.
- `/p/<plugin>/<page>/` serves a built page.
- `/hub/table`, `/hub/tool/<name>` and `/hub/contract/<c>` answer which node and plugin serve a name.
- `/hub/plugins`, `/hub/reload`, `/hub/install` and `/hub/uninstall` back the native verbs.
- `/usage` takes and reports usage events.

The control plane binds loopback only. A plugin proves itself with its service token, never with its address.

## Plugins

### The manifest

Each plugin folder declares itself in `awm-plugin.toml`: its services, the tools it publishes by unique name with major and tier, its pages, and what it needs (secrets, network, platform, the tools and contracts it calls). The plugin contract defines the format (`docs/redux/drafts/plugin-contract.md`). A manifest names no host. The install config places plugins on nodes.

The loader (`loader.rs`) scans the core roots first, then the plugin roots, then `<AWM_DIR>/installed/`. The core roots default to the tree's `core/plugins`, `awm/services` and `awm/plugins` under the workspace (`config.rs` `TREE_ROOTS`), so a node's `config.toml` names only the roots outside the tree. The first root that holds a name wins, and a later folder of that name is reported as shadowed. It refuses a plugin for bad TOML or an unknown key, a path outside its folder, a missing `run.sh`, a wrong platform, a tool another plugin serves, a reserved name outside a core root, or a need nothing satisfies. A refusal cascades to every plugin that needs the refused one. `awm plugins` lists what loaded, what was refused and why.

- **Reserved names.** `config.rs` `DEFAULT_RESERVED` plus every singleton tool in the edge list. A node that does not host a singleton can therefore never serve it locally.
- **Enable state.** `<AWM_DIR>/enabled.json` maps a service to true or false and overrides its profile. A service absent from it follows its profile. A disabled service's tools leave the catalog.
- **The guard.** `test/contract/test_plugin_contract.py` walks the tree and fails a manifest that breaks the contract, a secret the code reads but `[needs]` does not declare, and an import of another plugin's code.

**CAUTION** A plugin's `install.sh` and `run.sh` find their own folder from `$0`, and awm's tree from `$AWM_ROOT`. A path such as `$WS/awm/services/<name>` breaks the plugin the moment it loads from a root outside the tree.

### Roots outside the tree

A node can run plugins that live outside the awm checkout, so a private project keeps them in its own repo and they never reach GitHub or another node. Three lists name the extra roots, and they must agree:

- `config.toml` `plugin_roots`, which the gateway loads from;
- `AWM_SERVICE_ROOTS`, which `awm/install.sh` reads to run each plugin's `install.sh`;
- `AWM_PLUGIN_ROOTS`, which `awm/scripts/build.sh` reads to build each plugin's pages. `scripts/release-pages.sh` (run by `scripts/refresh-pins.sh` in the deploy) packs only the tree's own plugin pages into `scripts/pages.pin`, so a page of a plugin outside the tree reaches a push node by no deploy route.

A root missing from the first list loads nothing. A root missing from the second loads a plugin with no `.runtime-env`, and one missing from the third serves no page.

The owning repo provides the rest of the contract. A Python plugin ships an `install.sh` that runs `pip install -e` on its folder into `$AWM_ENV` and writes the `.runtime-env` sidecar. It reaches awm paths through `$AWM_ROOT`, not through `git rev-parse`, which names the owning repo. A plugin that execs a binary needs no `install.sh`. The owning repo gitignores `dist/` and `.runtime-env`.

**CAUTION** A page outside the tree finds no `node_modules` above its source. The `awm-resolve-from-tree` plugin in `vite.config.ts` resolves its bare imports from this tree. It also drops the svelte plugin's `resolve.dedupe` for such a page, because dedupe resolves `svelte` from the Vite root, which is the page folder. An in-tree build is unaffected. Diff a built in-tree `dist/` before you change either rule.

### Installed plugins

`awm install <source>…` installs or updates plugins as one change. A source is a folder or `git:<url>@<40-hex commit>`. A git source needs a passkey grant: an `operations approval_request` of kind `plugin_commits`, which the gateway checks against `AWM_APPROVER_GRANT_PUBKEY`. The plugins land in `<AWM_DIR>/installed/<name>`, pinned in `installed.toml`. When the reload refuses any of them, the whole change rolls back. `awm reload` applies a changed tree with no gateway restart and restarts only the plugins the caller names, and `install` names the plugins it replaced. `awm deploy --reload-restart <names>` hands its names to `awm reload --restart`. When the unit restarts too, the names restart after the new gateway answers.

### The service contract

The gateway starts each service as `bash run.sh` with a cleared environment. It passes an allow-list of inherited variables, `config.toml` `[env]`, the secrets the manifest declares, the proxy variables of § *Egress*, and these: `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_PLUGIN`, `AWM_NODE_NAME`, `AWM_DIR`, `AWM_PORT`, `AWM_WORKSPACE` and `AWM_HUB_TOKEN_FILE`. `supervisor.rs` `spawn` is the list.

`AWM_HUB_TOKEN_FILE` names a fresh service token per spawn, at `<AWM_DIR>/tokens/<service>.token`. An adopted service (§ *Service lifecycle*) keeps the token of its spawn. The service registers with it as `X-Awm-Service-Token`, opens its control WebSocket and sends `ready` with its API manifest. Every call it makes to `/invoke` carries the same token, which tells the gateway which plugin is calling.

- **Python services** build on `awm.gatewayclient.ServiceAdapter`, which handles register, ready, dispatch and reconnect. `call`, `subscribe` and `acquire_lease` take `peer=<node>` to run across an edge.
- **Per-service DBs.** `awm.persistence.databases` gives each service a DB at `<AWM_DIR>/services/<svc>/<svc>.db`. Each service seeds itself through its own `seed.py`.
- **Install.** Install each plugin through its own `install.sh`, never with hand-rolled `pip`. `awm/install.sh` installs the libraries, then runs every plugin's `install.sh`. Set `AWM_ENV` to change the target env.

## The catalog

### One tool per plugin

A plugin publishes the tools its manifest declares, and every function a service registers is published as `<tool>_<verb>`. `Hub::ready` (`hub.rs`) enforces this when a service sends `ready`. It strips the longest declared tool name that prefixes a function, and refuses the whole service for a function that fits no tool or a verb another service already holds. A refused service registers nothing. Two services of one plugin may share a tool.

A tool name holds letters, digits and hyphens. An underscore would make the split between tool and verb ambiguous.

A manifest function may carry these keys:

- `"params"` declares the verb's arguments. A verb with no `params` accepts anything, because no schema is not a promise of no arguments. Declare `params` on every new verb.
- `"timeout"` sets the verb's budget. See § *The timeout ladder*.

### The MCP view

`GET /tools` lists one entry per verb. `GET /tools?view=domains` folds the same catalog into one `{verb, args, peer}` tool per published tool, which `awm mcp` lists. Every MCP client therefore carries the small surface, while the CLI and HTTP reach every verb. A tool with `tier = "off"` leaves the folded view and stays in the flat one.

- **describe.** `describe` is reserved on every tool and answered from the catalog with no service round trip. `awm(verb="describe")` returns every tool with its verbs, a one-line summary and the required arguments. `describe` on a tool lists its verbs and params, and takes `verbs` as a filter.
- **Strict arguments.** The gateway refuses an argument the verb's `params` do not declare, with 400 and the verb's schema. Names that start with an underscore belong to the gateway.
- **The `peer` key.** Its enum lists `local` and the nodes across this node's edges, and its description names this node as the default. A call with no `peer` to a tool no local plugin serves runs where `/hub/table` says it lives.

**CAUTION** Tool schemas freeze when a session starts, so the `peer` enum is as of launch. The enum is discovery, not enforcement: dispatch checks against the live table.

## The timeout ladder

An unanswered request is not an unreachable daemon. Each number answers its own question.

1. A function's `"timeout"` is how long the verb may run, 120 s by default. The gateway answers 504 past it.
2. `tool_wait_s` (10 s) is how long a call waits for a tool whose service is still starting. The gateway answers 503 past it.
3. `reconnect_window_s` (10 s) is how long a service the gateway spawned may stay off its control socket before the supervisor restarts it. `adopt_window_s` (60 s) is the same limit for a service the gateway adopted, and it matches the adapter's give-up deadline.
4. An edge call times out at 60 s in `awm-edge`.

A cancel travels one hop at a time. When the gateway drops a call, it sends `{kind: "cancel", id}` to the service. The Python adapter trips the call's `awm.gatewayclient.cancel` token, which kills the process group of every subprocess started through `cancel.run`. A sync handler thread cannot be interrupted, so a handler is cancellable only where it runs subprocesses that way. A timeout is not a cancel: work that outlives its budget still finishes in the service.

## Verb gating

`Gateway::dispatch` (`gateway.rs`) applies three gates, in this order, to every call:

1. **Mode.** A session that cch started carries a mode, and `modes.rs` lists what each mode may call. The one restricted mode is `unknown`: it may run `reflection compact` and `whoami`, name no `peer` and open no stream. Every other mode, `worker` included, is unrestricted.
2. **Level.** `awm_trust::call_refusal` (`core/crates/awm-trust/src/trust.rs`) holds a cooperative or untrusted caller to its list (`docs/redux/channels.md` R125). A trusted caller passes.
3. **Private marks.** `privacy.rs` refuses a call from another node that names a project, scope or path this node marks private (`FEDERATION.md` § *Private projects*). It filters emits and session init frames the same way.

The native writes (`reload`, `install`, `uninstall`, the service lifecycle and `config_set`) also need a trusted caller on this node.

The gateway finds the caller through a pid: `X-Awm-Session-Pid` from `awm mcp`, a stdio child of the session, or `X-Awm-Caller-Pid` from the CLI. `caller.rs` walks up to eight ancestors and asks `reflection session_of` for each. The first session found is the caller, with its level, mode, scope and R9 id. A chain no harness claims is a user's shell and passes as trusted. A lookup that fails runs as untrusted with mode `unknown`. A positive answer is cached for 5 s.

**CAUTION** The mode gate is a guardrail on the agent-facing doors, not a trust boundary. The loopback gateway is open, and a request with no pid header carries no mode. A new restricted mode holds only while its session cannot make a raw HTTP request, so its launch must withhold Bash and WebFetch. The gate cannot cover Claude's built-in tools either.

Two gates sit outside the gateway. `awm edge-serve` admits a call only along an edge, and `awm port` runs only the tools of this host's singleton door (`FEDERATION.md`).

## The isolated door

A session in a container (`docs/redux/fleet.md` R109) reaches the gateway only through `/mcp` with `X-Awm-Caller-Token`, on `isolated_listen` or the main port. `isolated.rs` asks each local `harness@1` provider which session holds the token, and that session's whitelist bounds both `tools/list` and `tools/call`. A hard ceiling sits over every whitelist: no `ssh`, no harness tool, only reflection's own-session verbs, and no call to another node. The door pins `project`, `scope` and `to_scope` to the session's own. The token works at that door and nowhere else.

## Egress

Each plugin gets its own loopback proxy port, and the gateway points the plugin's `HTTP_PROXY` and `HTTPS_PROXY` at it. `egress.rs` lets through only the hosts the plugin's `needs.network` lists, checked on each connection. Loopback, the gateway's host and the fleet's nodes are exempt.

**CAUTION** The proxy holds only a client that honours the proxy variables. A client that opens its own sockets reaches the network unchecked.

## The native tool

The gateway serves the `awm` tool itself: `status`, `plugins`, `services`, the service lifecycle, `reload`, `install`, `uninstall`, `usage`, `table` and the config verbs. `native.rs` and `NATIVE_VERBS` in `gateway.rs` hold them. The CLI's core commands are thin calls to the same verbs. Run `awm --help` for the command tree.

## Usage per plugin

The gateway counts each plugin's use by surface (`docs/redux/drafts/plugin-contract.md` § *Usage event*): every call other than `describe`, and every page load. A plugin reports the two kinds the gateway cannot see through `awm.usage`:

- **Background runs.** `record_background` or `background_run` sends `{"kind": "usage", ...}` on the service's control WebSocket. Outside a service, or for another plugin, it posts to `POST /usage`.
- **Code use.** `record_code` posts a `code` event when another project calls a plugin's code directly.

The gateway appends each event to `<AWM_DIR>/usage/<YYYY-MM-DD>.jsonl` and prunes files older than 60 days.

- **CAUTION** The control frame carries no plugin. The gateway takes the plugin from the service, so a service cannot count use against another plugin.
- **CAUTION** Report a run of real work, never an idle tick. A loop that ticks every few seconds floods the log.
- **WARNING** A plugin's tests must capture its events, as the `usage_events` fixture in `transcripts`, `mic` and `rlm-factorio` does. An uncaptured event posts to the gateway on `AWM_PORT`, which on a node is the live one.

## The search archive

The `kb` plugin serves the `search` tool (`core/crates/awm-kb-search`). Agents get one search over everything the swarm knows, on this node and across its edges. search is a federator with no content store, because a second copy of each source drifted from the first. `core/plugins/kb/INSTALL.md` covers a node's install. `docs/search-opt/` holds the engine's decisions. This section holds the reasons.

- **kb's sources.** journals, guide and transcripts answer in-process through `awm_kb_source::Source`. Each owns its LanceDB store under `services/kb/lance/<source>`, through `awm-index`, and applies the caller's veil itself. No caller sees a row a mark covers.
- **Plug-ins.** Another plugin joins with one `[[service.archive]]` table per source in its manifest, with `mode = "live"`. The table names the verb search calls at query time, so a new source needs no change to search. A caller names the source as `name`, or `name@node` on another node. A source marked `optin` runs only when a caller names it. `awm services` lists the tables under `archive` in each row. An `index` table stays listed with an error, because search no longer reads change feeds.
- **Declared fields.** Each table declares its fields with a class and the fields a hit shows. The declaration is the query schema, so a new field needs no code in search.
- **The guide source.** It indexes this node's tools, one entry per verb, so an agent finds a verb by what it does. It follows the gateway's `plugins` and `services` topics, so a new plugin's verbs appear with no timer.
- **One store per source.** Each source answers apart, so no score has to compare across sources. A source's `$about` ranking fuses its own vector and keyword lists by score.
- **Maintenance.** Each write counts the rows it leaves outside the indexes. The count starts maintenance, not a clock.

**CAUTION** An open tag clash between two nodes stops every search between them until an agent renames the tag. Delete a peer's duplicate tag rows before a node's first start, or that start raises the clash.

## Service lifecycle

`supervisor.rs` starts every enabled service once the gateway binds, and holds it up. `survival.rs` holds what lets a service outlive the gateway.

A gateway restart is not a stop. `awm.service` sets `KillMode=process`, SIGTERM ends only the gateway, and the services keep running. `awm stop` is the one deliberate stop. It calls the `drain` verb, which stands every service down, empties the journal and ends the gateway. A plain `systemctl stop awm` therefore leaves the services running too, and each exits on its own after the adapter's deadline.

- **The journal.** `<AWM_DIR>/services.json` holds, per service, its pid, process group, process start time (`/proc/<pid>/stat` field 22, which guards against pid reuse), service id and breaker state. The supervisor writes it on spawn and on register. A deliberate stop removes the entry before it signals anything. Teardown writes use update-if-exists, so a loop that reports its exit late cannot bring a removed entry back.
- **Adoption.** Before it spawns anything, the gateway matches each journaled pid and start time against `/proc`. A live match becomes a hub record with no lease and keeps its token file. The service's own register, with the correct token, takes the record over. A 409 answers only while a live control socket holds the name. An adopted pid cannot be waited on, so the supervisor polls `/proc`. A service with no live journal entry spawns fresh. A journaled pid is never spawned a second time.
- **Silence.** An adopted service off its control socket past `adopt_window_s` gets its process group SIGTERM, then SIGKILL, and the supervisor waits until the group is gone. Only then does it respawn. Any respawn clears the old group first, so a leftover child cannot double.
- **Respawn.** A service that exits restarts after a backoff that starts at 1 s and doubles up to 30 s. A spawned service off its control socket past `reconnect_window_s` gets SIGTERM, then restarts.
- **The breaker.** Five exits inside 60 s trip the service. It stays down, shown as `failed` and `tripped` in `awm services list`, and the trip survives a gateway restart. A stop that names the service, `awm services start` and `awm services restart` clear it. Each is a deliberate act.
- **Hold mode.** The gateway logs each start in `<AWM_DIR>/gateway-starts.log`. The fourth start inside 10 minutes comes up in hold mode: it adopts live services and spawns none, and it posts a blocked card through `operations request_human` once that tool is up. `awm hold` shows the flag and `awm hold leave` ends it, clears the log and starts the services that are down. `StartLimitBurst` on the unit is the outer backstop. `awm stop` clears the log.
- **One gateway per state dir.** `<AWM_DIR>/gateway.lock` is an exclusive lock opened close-on-exec, so a service never inherits it. A second gateway on the same dir refuses to start.
- **Duplicates.** A register under a name whose instance is live gets 409. A second socket on a held control channel closes with 4409, and an unknown id with 4404.
- **Teardown.** On `drain` the gateway sends `{"kind":"shutdown"}` on every control socket, waits 8 s, sends SIGTERM to each service's process group, waits 2 s, then sends SIGKILL.

**CAUTION** The first restart onto a build with this lifecycle still runs the old binary's stop path, so every service restarts once. Later restarts leave the services alone.

**CAUTION** A new gateway adopts a service only if its journal entry names a live pid that started when the entry says. A reboot clears every pid in the journal and keeps the breakers.

Each service runs in its own process group. Anything that acts on a service's pid has to think in groups: a Python service is `run.sh`, then its interpreter, and the gateway knows only the first.

### The ready-ASAP contract

A Python service registers and sends `ready` before `on_start` runs, then finishes initialising in the background (`gatewayclient/adapter.py`). `call` and `session.open` frames that arrive during initialisation wait on an event, bounded by `AWM_INIT_WAIT_S` (default 60 s). A caller sees a slow first call instead of an error. `on_start` is where `init_service_db` runs, so the gate also keeps handlers off a half-built database. A failed init propagates out of `run()` and the process exits.

**CAUTION** Do not await slow work before `run()` or inside `on_start`. Put long startup work in a task that `on_start` spawns.

### Service-side give-up

`ServiceAdapter.run()` exits 0 without retry on a stand-down signal: a 409 on register, a control close 4409 or 4404, or a `shutdown` frame. It also exits when the gateway stays unreachable past `AWM_RECONNECT_DEADLINE_S` (default 60 s, the gateway's `adopt_window_s`). The deadline counts from the first failure of the outage, not from the last inbound frame. An idle service receives no frames, so a control socket that stayed up for 5 s counts as a confirmed link, and a new outage starts its own clock and its backoff from 1 s. Without that, a second gateway restart inherited the first outage's clock and ended the service at once.

### Gotchas

- **Use `gatewayclient.SupervisedSubscription` for every emit subscription.** When an emitter restarts, its subscribers' streams can stay open on a connection whose keepalives still pass, because they only prove the gateway is alive. Three services once shipped the same naive loop and went deaf together. The helper reconnects, bounds staleness with a jittered idle deadline and reports `healthy`. Show `healthy` in the service's `status`.
- **Use `gatewayclient.spawn_supervised` for every long-lived background task.** A bare `asyncio.create_task` whose handle nobody reads leaves the service looking healthy with that capability gone if the task raised on its first line. The wrapper logs at ERROR and respawns. It treats a return as a defect, so a supervised loop must never exit. Check the shutdown flag and skip the tick.
- **A child that must outlive a drain or a reboot needs its own cgroup.** `KillMode=process` spares a restart, but `awm stop`'s process-group kill does not spare a child in the service's group, and every descendant inherits the cgroup however it forks or `setsid`s. Place a survivor outside the `awm` unit with `systemd-run --user` in a transient unit, as `claude-science` does. The inverse trap is the Claude daemon: a background session inherits the cgroup and environment of whichever process first started the node's `claude daemon`. `cch` refuses to start a session unless its keep unit's daemon runs. Either failure is invisible in dev: the process is simply gone after the next drain.
- **One gateway per port.** Sandboxes run in parallel on distinct ports.

## Dev sandboxes

`core/plugins/dev/sandbox/run.sh` runs this worktree's `awm gateway serve` with its state in `sandbox/.awm`. Each start rewrites the sandbox's `config.toml`: this tree is the core root, the `dev` plugin is excluded, the worktree's built core plugin binaries are linked into `sandbox/.awm/bin/`, and `[env]` carries this tree's `PYTHONPATH` and interpreter, so the sandbox runs the worktree's code.

| Worktree | Port |
|---|---|
| Production (the `awm` unit) | `7819` |
| `projects/awm/dev/` | `7821` |
| `projects/awm/web-ui/` | `7831` |
| `projects/awm/web-backend/` | `7841` |
| any other scope | `7851` |

A gitignored `sandbox/.env` can pin `AWM_PORT`. The CLI targets `AWM_GATEWAY_URL`, else `<AWM_DIR>/gateway.json`, else port 7819. Export `AWM_DIR` or `AWM_GATEWAY_URL` before you point the CLI at a sandbox, or it reaches prod.

## Data-layer internals

The `awm-dvcdata` crate wires DVC into scopes. The `dvc` plugin owns its verbs and `scopes` links it for provisioning. `PROTOCOLS.md` § *Data* and `AGENTS.md` § *Data* cover usage. `provision_scope_data` is the only entry point.

awm wires and DVC operates. Wiring is the cache path, the merge driver, the hooks and the mount list. `dvc add`, `dvc checkout`, `git commit` and `git merge` run unwrapped.

- **The opt-in is the checkout.** `is_dvc_repo(p)` asks whether the worktree tracks a `.dvc/config`. There is no config table, no flag and no conversion verb. `AWM_DATA_DVC=0` is the global kill switch.
- **The cache path goes in `config.local`, absolute.** A tracked relative path resolves against a different base in a non-scope checkout, and DVC then starts a second cache there without an error. `config.local` is untracked, so it is per-machine.
- **`cache.type = hardlink,symlink`.** There is one physical copy per machine. A materialised file is a hardlink to the cache object. Nothing here ever runs `chmod +w` on a file, because the write bit belongs to an inode every scope and every commit reads. `chmod_dirs_writable` touches directories only, as the rmtree fallback.
- **Hooks go in the common git dir, by hand.** `dvc install` writes to `<root>/.git/hooks`, and in a secondary worktree `.git` is a file. Every awm scope is a secondary worktree. A post-commit hook exists beside post-merge because a conflicted merge fires no post-merge hook, and that is when a human has just hand-edited a pin. Both hooks are shared across worktrees, so their `[ -d .dvc ] || exit 0` guard is load-bearing.
- **git ignores a hook's exit status.** A failing `dvc checkout` removes the old files before it finds it cannot install the new ones, and it cannot fail the merge. It leaves a sentinel that `dvc scope_status` and provisioning report.
- **An absent mount list means everything. An empty one means nothing.** Treating them alike pulls every cold chunk in the project onto disk.
- **DVC's site cache goes on tmpfs.** `config.local` also sets `core.site_cache_dir` to `/dev/shm/awm-dvc-<uid>`. DVC builds a SQLite index per worktree and commits it row by row, so on a busy disk a new scope's checkout waits on one journal flush per entry. On altair that cost 400 s for 2k files, against 2 s on tmpfs. A node without `/dev/shm` keeps DVC's default under `/var/tmp` and pays the flushes.
- **A shared chunk costs one symlink per scope.** Every file in a pinned directory is one index entry and one inode per scope, so a reference database of 28k files multiplies both. A chunk listed in the tracked `data/.awm-shared` is checked out once per node per pin md5, renamed into `projects/<project>/data/<chunk>/<md5>/`, and linked from each scope. The pin stays in the commit unchanged, so the commit still names its data. That is also why `data` is a reserved top-level scope name.
- **A shared link must never point at another md5.** DVC follows the link, so a bare `dvc checkout` over a stale link rewrites the copy every other scope reads. The hooks repoint a link or remove it, never leave it stale. post-checkout exists for this alone and runs no `dvc checkout`. Shared copies have read-only directories as a second guard, and provisioning re-protects their files because DVC makes a file writable when an unlink is refused.
- **Shared copies are never collected.** gc keeps a cache object while a pin references it, and a superseded copy in the store holds its inodes until someone removes it.
- **The `gc` verb never runs `dvc gc`.** `dvc gc -p` holds the repo lock of every listed worktree for its whole run and re-walks the shared history once per worktree. One dry run locked ~100 worktrees for eight hours. `dvc gc` reads the pins itself, once per bare repo, and sweeps `files/md5` with no repo lock.
- **CAUTION** The grace window is the only race guard. `dvc add` writes cache objects before git can see any pin for them, so an unreferenced object younger than `GC_GRACE_DAYS` survives. Shortening the window shortens the time an unstaged pin is safe.
- **A missing `.dir` manifest blocks gc.** Its children cannot be told apart from garbage. Restore it from the archive, or name it in `accept_missing`. Every wired project must be named too: in `projects` to keep its data, in `exclude` to drop it.
- **Teardown guards uncommitted work, not content.** Deleting a worktree unlinks names, never bytes. Only uncommitted work dies, and `git status` covers data and code at once.

## Frontend

A shared component is a source folder imported by name, with no per-unit manifest. There is no npm workspace, no per-component `package.json` and no per-page `vite.config.ts`.

- **Components** live at `awm/ui_components/<name>/`: `src/*.svelte`, `.ts` and `.css`, plus a `src/index.ts` barrel for the public surface. Import them as `@awm/<name>` or a subpath such as `@awm/primitives/style.css`.
- **Pages** live in their plugin, at `<plugin>/pages/<name>/`: `index.html`, `src/main.ts` (which calls `mount(App, …)`) and `src/App.svelte`, plus optional `src/lib/**` and `src/styles.css`. The manifest's `[[page]]` names each one.
- **Third-party deps** go in the root `awm/package.json` only.

Resolution is one rule in two places, and they must agree:

- The bundler: root `awm/vite.config.ts` aliases `@awm/<name>/<sub>` to `ui_components/<name>/src/<sub>` (subpath rule first) and `@awm/<name>` to `ui_components/<name>/src/index.ts`.
- The typechecker: `awm/tsconfig.json` `paths` carry the same `@awm/*` glob, so `tsc` and `svelte-check` follow cross-component imports without a build.

**CAUTION** Vite honors package `sideEffects` only under `node_modules`, and treats aliased first-party source as side-effectful. Without help, every barrel re-export would bundle. The root config's `build.rollupOptions.treeshake.moduleSideEffects` marks component `.ts` and `.svelte` source side-effect-free, while query-bearing virtual CSS modules and real `.css` keep their side effects. A page then bundles only what it imports, and a used component's `<style>` survives. Before you change this rule, diff a built `dist/` against a known-good build.

### Serve contract

The gateway serves a page's built `dist/` (the `[[page]]` `dir`, default `pages/<name>/dist`) at `/p/<plugin>/<page>/`. A path with no extension falls back to `index.html`. A page with no built bundle is reported and not served. Nothing in the serve path reads frontend source or config. `awm/scripts/build.sh` builds every page, and `awm deploy` runs it.

A page reaches the gateway through `@awm/client`: `tool(name).call(verb, args)` posts to `/invoke`, and `openSession` opens `/hub/session/<service>/<kind>`. The needs gate knows the page's plugin by its `Referer`, so a page calls only what its plugin's manifest lists.

**CAUTION** The gateway serves a loaded plugin's pages whether or not its service is enabled. A page whose service is down loads and then fails its calls.

## Federation

Read `FEDERATION.md` before you touch anything cross-node. The loopback gateway stays open and unauthenticated. A call to another node goes from the local gateway down an ssh channel to that node's loopback gateway (`core/crates/awm-edge`). The edge list (`awm.config.edges`, `awm_trust::edges`) is the only record of who reaches whom.

- This is not the retired federations: neither the v0 one (cr-sqlite replication, leader election, a `peers` registry) nor the peer book with node tokens and a private CA. The git history of their deletion is not a guide to the current system.
- The gateway trusts `X-Awm-Caller` from `awm edge-serve` only beside the edge token in `<AWM_DIR>/edge.token`, which it rewrites at each start. It trusts `X-Awm-As` because the edge (`auth`) strips every inbound caller header (`X-Awm-As`, `X-Awm-Caller-Pid`, `X-Awm-Session-Pid`) and stamps its own. A new edge mount must do the same.
- A cross-node call from a service passes `peer=<node>` to `awm.gatewayclient`. A hand-rolled local POST skips that branch, and the call silently runs on the wrong node.
- A reload that adds, removes or restarts a plugin sends `_table_changed` across each edge, so the other nodes refetch this node's table. They cache it in `<AWM_DIR>/peer-tools.json`. A peer that gets the notice re-resolves `needs` only: it starts the plugins whose `needs` now resolve and stops those whose `needs` no longer do. It never runs a full reload and never forwards the notice.

## File map

Paths are under `core/crates/`.

- **Gateway state and dispatch** — `awm-gateway/src/gateway.rs`: the catalog, `describe`, `dispatch` and its gates, the native verbs.
- **Routes** — `awm-gateway/src/server.rs`. Streams in `streams.rs`, mounts in `mount.rs`.
- **Plugin control plane** — `awm-gateway/src/hub.rs`: register, ready and the tool rule, calls and cancels.
- **Loader and config** — `awm-gateway/src/loader.rs`, `config.rs`. Installs in `install.rs`.
- **Supervisor** — `awm-gateway/src/supervisor.rs`.
- **Callers and gates** — `awm-gateway/src/caller.rs`, `modes.rs`, `privacy.rs`, `isolated.rs`, `egress.rs`. Levels in `awm-trust/src/trust.rs`.
- **Edges** — `awm-trust/src/edges.rs` reads the edge list, `awm-gateway/src/peer.rs` routes a call to another node, `awm-edge` carries it, and `awm-cli/src/edge.rs` serves `awm edge-serve` and `awm port`.
- **CLI and MCP** — `awm-cli/src/lib.rs`, `deploy.rs`, `bootstrap.rs`. `awm-mcp/src/lib.rs`.
- **Wire and manifest types** — `awm-proto`.
- **Search archive** — `awm-kb-search`. `registry.rs` reads the plug-in tables, `merge.rs` fuses and `federation.rs` asks each other node across its edge. `awm-index` is the engine each source links.
- **Frontend** — `awm/vite.config.ts`, `awm/scripts/build.sh`, `awm/package.json`.

## The editable install

The awm env's editable installs map each Python package to `/home/tony/agentic_workspace/awm`, the release tree. `import awm.<pkg>` from the env therefore loads release code, not your worktree's. Pick one workaround:

- Set `PYTHONPATH=<your-worktree>` to shadow the mapping. `scripts/run-tests.sh` does this.
- Run a dev sandbox, whose `config.toml` `[env]` carries the worktree's `PYTHONPATH`.
- Land the change on release. The deploy moves the mapping's target.

**WARNING** Never run an `install.sh` or `pip install -e` from a worktree into the `awm` env. It repoints the live services' packages at your branch.

## Running tests

Each Python dist owns its own `tests/` directory and `pyproject.toml`. All dists merge into the `awm` namespace, so one `pytest` over the tree cannot import them all. `scripts/run-tests.sh` runs pytest once per dist, with that dist's source root and the shared components on `PYTHONPATH`:

```bash
scripts/run-tests.sh                        # every dist. Run before merging
scripts/run-tests.sh scopes contract        # named dists only
PYTEST_ARGS="-x -q" scripts/run-tests.sh    # extra pytest args
```

It reports pass or fail per dist and exits non-zero if any failed. The `contract` dist is `test/contract/`.

- Make cross-dist imports in a test lazy, inside a fixture or function. A top-level cross-dist import brings back the namespace shadowing the runner avoids.
- Add a new plugin with tests to both `DISTS` and `ORDER` in the runner. A dist missing from `DISTS` reports `unknown dist` and never runs. `reflection` sat that way for months.

Run the Rust tests with `scripts/rust-build.sh test --workspace`, which runs cargo in a container. Its build output goes to `~/.cache/awm-target/<worktree>`, outside the worktree, because each target holds 4 to 5 GB. Run `scripts/rust-build.sh --gc` to list the target dirs of worktrees with no live session, and add `--yes` to delete them. `test/install/run.sh` installs awm in a rootless container and joins it to a fleet. Run it when a change touches install, reload or the isolated door.
