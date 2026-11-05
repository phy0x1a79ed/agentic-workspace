# Plugin contract, draft v1

## Purpose & Contents

This file is the contract between a plugin and the Rust gateway of the redux (`install.md` R112, R115 to R117, R132). It holds the manifest format, the wire protocol a service speaks, the call interface a plugin uses to reach another plugin, and the usage event. The redux-core lane (job a92d02bf) owns it. Other lanes build against it. Send a change request to `redux-build/core` with `SendMessage`.

Status: draft v1.11. Build against it. v1.11 is the cutover (T19): the Rust gateway is the only gateway, the legacy loader goes, streams and sessions get their own routes and cross the edge, a service may mount its own HTTP server, a page names its bundle, the gateway stamps `_caller_pid` and hides private marks from peers. v1.9 enforced `needs.secrets` and added the install from a folder with its pin record (R116 items 1 and 2). v1.10 drops `trust` (R116 item 4), adds `verbs` to each need and enforces it (R116 item 3), enforces `needs.network`, adds the git install with its signed approval (R115, R116 items 1 and 2), replaces `X-Awm-Plugin` and the `/svc` door with the service token, makes a failed caller lookup untrusted (`fleet.md` R103 item 2) and sends every peer call through the local gateway (R86, R92).

## Plugin folder

A plugin is a folder. Its top level holds one manifest file, `awm-plugin.toml`. The gateway loads a plugin from any plugin root, in the awm repository or outside it. An install copies a plugin folder into the state's installed root, then loads it the same way (§ Install).

A folder with no `awm-plugin.toml` is not a plugin. The gateway ignores it.

Plugin roots are an ordered list in the gateway config. The built-in core roots are `awm/services` then `awm/plugins`. Put a new core plugin at `awm/plugins/<name>/`. The first root that holds a plugin name wins. The gateway logs and ignores a shadowed folder.

A tool name on the reserved list of the gateway config belongs only to a plugin from a core root. Any other claim refuses the plugin. A plugin's trust is where it loads from, never a manifest key. The `awm` tool belongs to the core: the gateway serves its verbs itself. The default list is `awm`, `auth`, `2fa`, `approver`, `queue-store` and `journal`, plus the singleton tools of the edge list (§ Install config).

## Manifest

Example, for a plugin that serves one MCP tool, one hidden tool and one page:

> ```
> [plugin]
> name = "journal"
> version = "1.4.0"
> refresh = "./refresh.sh" # optional R33 hook, run in the plugin folder
>
> [[service]]
> name = "journal"
> path = "."               # folder that holds run.sh, relative to the plugin
> run = "run.sh"
> install = "install.sh"   # default. The deploy runs it, as today
>
> [[service.archive]]      # today's service.toml [[archive]], unchanged
> name = "journals"
> mode = "index"
> export = "journal_export"
> topic = "posts"
>
> [[tool]]
> name = "journal"         # unique across the fleet, the tool's permanent id
> major = 1
> service = "journal"
> tier = "on"              # on: on the MCP surface. off: gateway and CLI only
>
> [[tool]]
> name = "journal-admin"
> major = 1
> service = "journal"
> tier = "off"
>
> [[page]]
> name = "feed"
> dir = "pages/feed/dist" # optional. The default is pages/<name>/dist
> entry = "index.html"
>
> [needs]
> secrets = ["JOURNAL_DB_KEY"]
> network = ["github.com", "*.googleapis.com", "api.example.org:8443", "tcp:22"]
> platform = ["linux"]     # optional. Empty is any
> gpu = false              # optional. Recorded, not enforced yet
>
> [[needs.tool]]
> name = "precedence"      # the tool this plugin calls
> major = 2
> verbs = ["search", "add"] # every verb it calls, describe excepted
> ```

Rules the loader enforces:

1. A tool name is letters, digits and hyphens. It holds no dot and no underscore. Use `harness-cc`, never `harness.cc`.
2. A tool name is unique across the fleet and is the tool's permanent id (R117 item 2). A name that two plugins claim refuses the second plugin and names the owner (R116 item 5). A fork settles a clash by renaming the tool in its own manifest. The install config renames nothing.
3. A `needs.tool` name that no plugin serves, on this node or a peer, refuses the plugin. The refusal names the tool. A `needs.tool` or `needs.contract` with no `verbs`, or with a verb that breaks the tool name rule, refuses the plugin too.
4. A `needs.tool` major that differs from the owner's major refuses the plugin.
5. Refusals cascade: a plugin that needs a refused plugin's tool is refused too.
6. `tier = "off"` keeps a tool off the MCP surface. Plugins and the CLI still call it through the gateway.
7. Each `[[tool]]` names the services of the same plugin that serve it: `service = "<name>"`, or `services = ["board", "reporting"]` when several share it. A plugin with one service may omit both. A service publishes each function of its `ready` manifest as `<tool>_<verb>`, as today. Two services that publish one verb of a tool refuse the later one at `ready`.
8. A plugin whose `needs.platform` excludes this node's operating system is refused.
9. A `page.dir` or `service.path` that leaves the plugin folder refuses the plugin. A page whose bundle is not built yet is not served, and the loader warns. The rest of the plugin loads. The gateway serves a page at `/p/<plugin>/<page>/`. `awm/scripts/build.sh` builds every `pages/<name>/` folder of every plugin.
10. A service gets only the secrets its plugin's `needs.secrets` lists (§ Service wire protocol, step 1).
11. Each `needs.network` entry must parse (§ Network). A bad entry refuses the plugin.

### Verbs

A plugin calls only the verbs its needs list (R116 item 3). Both gateways check each call at dispatch, after the mode gate.

1. The gateway knows the calling plugin from the service token (§ Service wire protocol, step 2), or from the `Referer` of a page under `/p/<plugin>/<page>/`. A token that no spawn got gets 401.
2. The plugin's own tools pass. `describe` passes on any tool it needs.
3. A verb that no `[[needs.tool]]` or `[[needs.contract]]` lists gets 403. The refusal names the plugin, the tool and the verb, and says to add the verb to the manifest.
4. A call with no service token and no page `Referer`, such as a session's or the CLI's, is not checked. The core's own `awm` tool needs no entry.
5. A stream (§ Streams and sessions) from a plugin needs a `[[needs.tool]]` of a tool the streamed service serves, with any verb.

**CAUTION** A missing verb breaks a plugin only at the call. `test/contract/test_plugin_calls.py` replays every plugin's recorded calls against its manifest. Add each new call to its table.

### Network

The gateway enforces `needs.network` (R116 item 1). It runs one loopback egress proxy for each plugin, and sets `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` for the plugin's services. `NO_PROXY` holds loopback, the gateway's host and the nodes of the edge list.

1. `host` allows that host on ports 80 and 443. `*.host` allows its subdomains. `:port` after either names the one port allowed.
2. `*` allows every host and port. Use it for a plugin that browses wherever it is sent. The install review sees it.
3. `tcp:<port>` and `udp:<port>` name traffic the proxy cannot see, such as ssh or a wake-on-LAN broadcast. The gateway records them and checks nothing.
4. A refused request gets 403 with `add "<host>" to network in its [needs]`. The gateway logs `egress guard: refused plugin <plugin> <host>:<port>` at warn.

Known limits:

- A client that ignores the proxy variables is not checked. Python's `http.client`, `imaplib` and `smtplib`, aiohttp by default, discord.py, ssh, openconnect, Node without `NODE_USE_ENV_PROXY` and Chrome in a container all ignore them.


## Contracts

An interface that several plugins serve is a contract (`install.md` R117 item 4). Tony: "Option a. Reflection caller doesnt care about details".

1. A contract has a unique name and a major version, written `<name>@<major>`, such as `harness@1`. The name follows the tool name rule.
2. Each provider keeps its own tool name. It names the contracts it implements on its `[[tool]]`: `implements = ["harness@1"]`.
3. A dependent names the contract, never a provider: `[[needs.contract]] name, major`.
4. The contract resolves to every provider in the live table, zero or more. A contract name never clashes, because many providers are its purpose.
5. `GET /hub/contract/<name>@<major>` lists the providers now, as `[{name, major, plugin, peer}, ...]`.

Example: cch serves `harness-cc` and dsh serves `harness-ds`. Each implements the `harness` contract at major 1. reflection needs that contract and dispatches on the platform code of the session.

## Install

1. `awm install <folder>...` (`POST /hub/install {sources}`, the `awm` tool's `install` verb) copies each folder into `<AWM_DIR>/installed/<plugin>`. The copy leaves out `.git` and the folders a stamp skips. A later edit of the source changes nothing until the next install.
2. All the folders of one install load as one change. A plugin the loader refuses rolls the whole change back, and the refusal names the reason.
3. A plugin name that a core root or another plugin root already loads is refused.
4. `<AWM_DIR>/installed.toml` pins each installed plugin: `name`, `source`, `commit` (the folder's git HEAD, when it has one), `image` and `installed_at`.
5. `awm uninstall <name>` removes the folder and the pin, then reloads.
6. A source `git:<url>@<commit>` names a git repository at a full 40-hex commit (R115). The gateway fetches that commit, checks that `HEAD` is that commit, and copies the tree without `.git`.
7. A change that holds a git source needs Tony's approval (R116 item 2). One approval covers the whole change. A local folder, a core or plugin root and the reload of a pinned plugin need none.

Only a trusted caller on this node may install or uninstall.

### Approval

1. The gateway reads the approver's public key `AWM_APPROVER_GRANT_PUBKEY` (base64url Ed25519) from the secrets file, then its own environment, then the install config's `env`. With no key it refuses the change with 403 and names the key, before it files any request.
2. The first install files `operations approval_request` with kind `plugin_commits`, this node, and the payload `{change, plugins: [{plugin, source, commit, from?}]}`. It loads nothing. It answers `{"installed": [], "approval": <request_id>, "commits", "next"}`.
3. Tony approves on his passkey. Run the same install again with `approval=<request_id>` (`awm install --approval`, the HTTP body's `approval`).
4. The gateway reads `operations approval_get` and checks the grant itself. The signature must verify under the key. The kind must be `plugin_commits`, the node this node, the payload digest this change's, the commits exactly this change's `name@commit` list, and now inside the grant's window. Any other grant loads nothing, and the refusal names the check that failed.

## Refresh hook

The deploy runs a plugin's `refresh` command when the files of the plugin folder changed between two releases (`routines.md` R33).

1. The deploy runs the command in the plugin folder.
2. It sets `AWM_REFRESH_FROM` to the old release commit, `AWM_REFRESH_TO` to the new one, and `AWM_NODE_NAME`.
3. Exit 0 means refreshed. A nonzero exit is a failed refresh, and the deploy dispatches a fixer.
4. A changed plugin with no `refresh` key falls back to the project hook, `deploy.toml` `[refresh]`.

## Install config

awm names no host (`fleet.md` R140). A manifest says what a plugin needs, never where it runs. The node's installer writes the install config, `<AWM_DIR>/config.toml`, and hands it to the gateway. The cloud project produces it per node.

The keys that place plugins:

1. `plugins` lists the plugin names this node loads. `["*"]`, the default, loads every plugin in the roots.
2. `exclude` lists the plugin names this node never loads.
3. `plugin_roots` adds roots after the core roots. `profiles` and `env` pass through to services.
4. `[[external]]` names a service the gateway serves but does not spawn, as `{service, token_file?}`. Another user's systemd unit runs it (R99, R107). See § External services.
5. `isolated_listen` is a second address that serves only the isolated door. See § Isolated sessions.

A reload reads the keys that place plugins again: `plugins`, `exclude`, the roots, `profiles` and `reserved`. A change to `listen`, `isolated_listen`, `peer`, `env` or `[[external]]` takes a gateway restart.

The `[fleet]` table holds the rollout order: `home`, `nodes` (starting with `home`), and an optional `[fleet.project.<name>]` with `canonical` and `nodes`. The routine plugin reads it. The gateway ignores it. The routine plugin also honours `AWM_INSTALL_CONFIG` as an override of the path, for tests and dry runs. The gateway reads only `<AWM_DIR>/config.toml`.

The edge list is node config too. The gateway reads `AWM_EDGES_FILE`, else `<AWM_DIR>/edges.yml`. It adds `[s.tool for s in singletons]` to the reserved list. A missing file is an empty fleet.

The loader warns on a `[placement]` table and ignores it.

## Service wire protocol

`awm.gatewayclient.ServiceAdapter` speaks this protocol.

1. The gateway starts each enabled service with `bash <run>` in the service folder. It sets `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_PLUGIN`, `AWM_NODE_NAME`, `AWM_DIR`, `AWM_WORKSPACE` and `AWM_HUB_TOKEN_FILE` in the environment, plus the install config's `env` and the egress proxy (§ Network). It never sets `AWM_SERVICE_ID`. The service inherits nothing else of the gateway's environment but `PATH`, `HOME`, the user, shell, locale and time zone, `TMPDIR`, `XDG_RUNTIME_DIR`, `TERM` and `AWM_CONDA_ROOT`. It also gets each secret its plugin's `needs.secrets` names. The value comes from the secrets file, `secrets_file` in the install config (default `<AWM_DIR>/secrets.env`, `NAME=value` lines), else from the gateway's own environment.
   **CAUTION** Every service sees the install config's `env`. Put a secret in the secrets file, never in `env`.
2. The service posts `POST /hub/service/register` with `{name, pid, start, cwd, mount?}` and the header `X-Awm-Service-Token`, read from `AWM_HUB_TOKEN_FILE`. Each spawn gets a fresh token, so no other local user can take the service's name while it is down. A register with a wrong token gets 403. The same token names the service on each `/invoke` and stream it opens (§ Calling another plugin). The reply carries `service_id`, `control_ws_path`, `bridge_ws_base` and `canonical_workspace`. `mount` is the loopback URL of the service's own HTTP server. The gateway serves it at `/s/<service>/`, HTTP and WebSocket, while the service is ready. A mount that is not a loopback `http://` URL gets 400.
3. The service opens the control WebSocket at `/hub/service/control/<service_id>`. It sends `{"kind":"ready","api":{"functions":[...]}}` first. Each function is `{name, description, params:[{name,type,required,description}], tool?}`.
4. The gateway sends `{"kind":"call","id","fn","args","as"?,"caller"}`. `caller` is `{"node","lane","session","trust","scope"}`: `lane` is `local`, `edge` or `service`, `session` is the R9 session id or null, `trust` is `trusted`, `cooperative` or `untrusted`, and `scope` is the session's own `<project>/<scope>` or null. Only the gateway fills `caller` (R103). It drops any `X-Awm-Trust` or `X-Awm-Lane` header a caller sends. A service that predates `caller` ignores it, and reads the caller in its args instead (§ Caller arguments). The service answers `{"kind":"reply","id","ok":true,"result"}` or `{"kind":"reply","id","ok":false,"error","error_class"}`. The gateway may also send `notify`, `cancel` and `shutdown`.
5. The service may send `{"kind":"emit","topic","payload"}` (§ Streams and sessions) and `{"kind":"usage",...}` (see § Usage event).
6. A closed control WebSocket releases the service. The gateway respawns it after a 10 s window unless the gateway is stopping.

A second instance of a live service gets 409 on register and stands down.

### Streams and sessions

1. `GET /hub/emit/<service>/<topic>` is a WebSocket. Each payload the service emits on that topic arrives as one JSON text frame. A subscriber sends nothing.
2. `GET /hub/session/<service>/<kind>` is a WebSocket to a direct session. The service lists its kinds in its ready api as `"sessions": [{"kind", "transport": "direct"}]`. The client sends the init JSON object as its first text frame. The gateway sends the service `{"kind":"session.open","session_id","session_kind","bridge_id","init","as"?,"caller"}`. The service answers `{"kind":"session.opened","session_id","ok","error"?}`, then opens `GET /hub/service/bridge/<service_id>/<bridge_id>`. The gateway relays the two sockets, text and binary, until either closes. A refusal closes the client socket with code 1011 and the reason.
3. Both take `?peer=<node>`. The stream then runs on that node over the edge, as a call from this node (§ Peer calls).
4. A session in a restricted mode may not open a stream.

### Caller arguments

The gateway also writes the caller's trust and scope into the args of every call, for a service that reads args only. The journal and the browser bound every verb by them.

- `_caller_trust` is the calling session's level, from reflection's `session_of` answer.
- `_caller_scope` is `<project>/<scope>` of that answer's `cwd`, as `awm.config.scope_of` reads it. It is absent outside a project.
- `_caller_pid` is the pid of the calling session on this node: the pid the gateway's walk ended on. Only a local call from a session carries it. The gateway replaces any `_caller_pid` a caller sends.
- `_caller_session` keeps its meaning: the `{kind, job, name}` claim of a session on another node. It survives only on an edge call.
- `_caller_session_id` is the R9 id of an isolated session. Only the isolated door writes it (§ Isolated sessions). The gateway drops it from every other call.

The gateway finds the session from a pid: `X-Awm-Session-Pid`, which `awm mcp` sends, or `X-Awm-Caller-Pid`, which the CLI sends for itself. It walks that pid's ancestors and asks `reflection session_of {pid}` for each, in-process through the hub, so the lookup is neither gated nor looked up itself. reflection asks the harness plugins (`install.md` R135 item 6). The gateway reads no harness's files.

1. A known session answers `{ok, known: true, session_id, mode, trust, cwd, ...}`. The first session found is the caller, with its trust, mode and scope.
2. A chain that no harness claims is a user's shell. It reads as `trusted` with no scope.
3. A lookup that fails reads as `untrusted`, with mode `unknown` (`fleet.md` R103 item 2). cch records each session at its start, so a session Tony starts by hand is found.
4. A call with no pid reads as `trusted`. Only a loopback caller can send one, and the gateway logs a warning.
5. A call with a valid service token is lane `service` and `trusted`, unless it also names a pid.
6. The gateway keeps a positive answer `session_cache_s` seconds (install config, default 5). It never keeps a failed lookup.

The gateway drops the two keys a caller sends. A service that finds no `_caller_trust` reads the call as `untrusted`.

The gateway gates each call by the session's mode, as `awm.config.modes` does. A mode it cannot read is `unknown`, which is restricted. A refused call gets 403.

Over an edge, the pair comes from the far node. The gateway takes it from the edge's `caller` frame, and stamps `as` = `peer:<node>` when the call names no principal. The gateway refuses a `_caller_trust` or `_caller_scope` that a model sends to `/invoke` as an unknown argument of a verb that declares its params.

### External services

A service in the install config's `[[external]]` list runs under its own system user, apart from the gateway's user. The gateway never spawns it. It waits for the service to register, then serves it like any other.

1. Run the service's `run.sh` from its unit, with `WorkingDirectory` set to the service folder. Set `AWM_HUB_URL` to the gateway's URL and `AWM_SERVICE_NAME` to the service name.
2. Set `AWM_HUB_TOKEN_FILE` to a file that holds the service's token.
3. Point the entry's `token_file` at a file with the same token, readable by the gateway's user.
4. Load the secrets its plugin's `needs.secrets` names with `LoadCredential=secrets.env:/etc/awm/secrets/<tool>.env`. Keep the source file root's alone (mode 0600, in a 0700 directory), holding only that plugin's names, as `secrets.env` lines. systemd gives the unit a copy only its user reads, and `awm.gatewayclient` puts the lines into the environment when the service imports it. The gateway's `secrets.env` does not carry them. `test/singletons/secrets.sh` proves one singleton cannot read another's secret.
5. Point `AWM_DIR` at the unit's own state directory. A service keeps its files, and fetches its binaries, under `AWM_DIR`.

## Isolated sessions

An isolated session runs in a container and has no host pid (`fleet.md` R109). cch writes its MCP config to call `<door>/mcp` with the header `X-Awm-Caller-Token`.

1. The door is `POST /mcp`, MCP over streamable HTTP with JSON replies. `GET` gets 405, because the door sends no server events.
2. The gateway asks each `harness@1` provider `caller_of_token {token}` who holds the token. A positive answer is kept 10 s, so a revoked token stops working within 10 s. No token, or an unknown one, gets 401. A failed lookup gets 503.
3. `tools/list` and `describe` show only the verbs on the answer's `tools`. A call to any other verb gets an `isError` result that names the whitelist.
4. The gateway also holds a ceiling that no token lifts: no `ssh`, no tool that implements `harness@1`, and no `reflection` verb but `whoami`, `compact`, `message_self`, `pending` and `mode`.
5. A call runs on this node only. A `peer` other than `local` or this node is refused.
6. `project`, `scope` and `to_scope` must name the session's own scope when sent. A verb that declares `project` or `scope` gets the session's value when the caller sends none.
7. The call carries the caller `{lane: local, session: <R9 id>, trust, scope}` and the args `_caller_trust`, `_caller_scope` and `_caller_session_id`. Trust is `untrusted`, or `cooperative` when the answer says so. A token never makes a session trusted.

The loopback listener serves `/mcp` too. `isolated_listen` in the install config binds a second listener that serves `/mcp` and nothing else. Bind it where the containers reach the host. cch's default URL is `http://host.docker.internal:8765`, so use port 8765 or set `AWM_ISOLATED_HUB_URL` in cch. `gateway.json` names the door as `isolated_url`.

**WARNING** A request on loopback `/invoke` passes as a trusted local caller. Keep the loopback listener out of the containers' reach. The gateway refuses a request that carries `X-Awm-Caller-Token` on `/invoke`.

**CAUTION** reflection and operations find the caller's own session from `_caller_pid`. They must read `_caller_session_id` when it is present, or their own-session verbs fail for an isolated session.

## Calling another plugin

A plugin calls another plugin only through the gateway (R117 item 1).

1. Call each tool you need by the name your `[[needs.tool]]` gives it, with a verb its `verbs` lists (§ Verbs). For a contract, read its providers from `GET /hub/contract/<name>@<major>` at call time.
2. Call `POST $AWM_HUB_URL/invoke` with `{"name":"<tool>","args":{"verb":"<verb>","args":{...},"peer":"<node>"?}}`.
3. Send `X-Awm-Service-Token` with the token from `AWM_HUB_TOKEN_FILE`. The gateway names your plugin from it and counts the call as surface `plugin`. A wrong token gets 401. `awm.gatewayclient.call` does steps 2 and 3.
4. Send `X-Awm-As: <principal>` when you act for someone, as today.
5. Read `{"result": <string>}` on success. The string is the verb's JSON result. An error is a non-2xx status with `{"detail": ...}`.

`GET /hub/tool/<name>` answers the tool's owner, `{name, major, plugin, peer}`, or 404. The `/svc` door is gone.

### Peer calls

A call to another node goes to the local gateway with `peer` (`fleet.md` R86, R92). There is no peer book and no peer token.

1. The gateway carries the call over the edge: `ssh <peer> awm edge-serve`. The far node runs it as a call from this node.
2. A `peer` with no edge from this node is refused.
3. The edge carries `/invoke` calls and streams (§ Streams and sessions). `awm.gatewayclient` `subscribe` and `acquire_lease` take a `peer`.
4. A verb that acts on the calling session itself, such as `reflection compact` or `operations hook_plan`, is refused with a `peer`: the far node has no calling session.
5. A file path in a peer call is a path on the far node. No call carries file bytes across nodes.
6. A call or stream from another node never names a project, scope or path this node marks private (`<AWM_DIR>/private.json`, `awm.config.privacy`). It is answered as an unknown tool, and an emitted payload a mark covers never reaches a peer.

A call to a tool that is not up yet waits up to 10 s, then fails with 503 and names the tool (R117 item 7).

## The name table and hot reload

The gateway keeps one live table from each tool name to the plugin that owns it, on this node and its peers (R117 item 3). Every call looks its name up in that table, so a plugin change takes effect at the next call (R144).

1. `awm reload`, the `awm` tool's `reload` verb or `POST /hub/reload` reads the roots and the install config again. Only a trusted caller on this node may reload.
2. The reload compares each plugin with the loaded one. It starts an added plugin, restarts a changed one and stops a removed one. Every other service keeps its process.
3. A plugin is changed when its folder's stamp moved or its services differ. The stamp covers the names, sizes and mtimes of the folder's files. It skips `.git`, `node_modules`, `target`, `__pycache__`, `.venv` and `.pytest_cache`.
4. To install a plugin, put its folder in a root and reload. To update it, change its files and reload. To rename a tool, edit the manifest and reload. To remove a plugin, delete its folder or add it to `exclude`, then reload.
5. After a change, the gateway posts `{node}` to each peer's `POST /hub/table/changed`. The peer reads `GET /hub/table` again, then reloads, because a need may now resolve or break.

`GET /hub/table` lists this node's tools as `[{name, major, plugin, implements}]`. The `awm` tool's `table` verb shows the whole live table, peers included.

## MCP and CLI surface

Each tool is one MCP tool with input `{verb, args, peer}`. `verb = "describe"` returns the verb schemas. `peer` names the node to run on. The MCP proxy (`awm mcp`) reads `GET /tools?view=domains` and posts `POST /invoke` with `X-Awm-Surface: mcp`. The CLI (`awm <tool> <verb> [--key value]...`) posts the same call with `X-Awm-Surface: cli`. `--help` prints the verb's schema. A verb word may spell `_` as `-`.

## Usage event

The gateway measures each plugin by the surface it is used through (R132). One event is one JSON object:

> ```
> {"v":1, "ts":"2026-11-03T22:10:00Z", "plugin":"journal",
>  "surface":"mcp", "tool":"journal", "verb":"post", "name":null,
>  "ok":true, "ms":12}
> ```

`surface` is one of `mcp`, `cli`, `http`, `plugin`, `page`, `code` and `background`. `tool` and `verb` are set for a call. `name` is the page name for `page` and the job name for `background`. `ok` and `ms` are optional.

Who records each surface:

| Surface | Recorded by |
|---|---|
| `mcp`, `cli`, `http`, `plugin` | the gateway, on each `/invoke`, from `X-Awm-Surface` and the service token |
| `page` | the gateway, on each HTML document GET under `/p/<plugin>/<page>/` |
| `background` | the plugin, as a control-WS frame `{"kind":"usage","surface":"background","name":"<job>","ok":true,"ms":840}` |
| `code` | the library, as `POST /usage` with the event body, for use straight from another project's code |
| `page` on its own origin | the plugin, as `POST /usage` with `surface = "page"` and the page `name`, for a page the gateway does not serve, such as the approver panel |

The Python helpers are `awm.usage.record_background(job, ok, ms)` and `awm.usage.record_code(plugin, name)`, in `awm/service_components/usage` (redux-plugins lane).

The gateway fills `v`, `ts` and, for a control-WS frame, `plugin`. It appends each event to `<AWM_DIR>/usage/<YYYY-MM-DD>.jsonl` and keeps a 60-day tally per plugin and surface. `GET /usage` and `awm usage` read the tally.
