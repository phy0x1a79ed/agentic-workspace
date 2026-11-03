# Plugin contract, draft v1

## Purpose & Contents

This file is the contract between a plugin and the Rust gateway of the redux (`install.md` R112, R115 to R117, R132). It holds the manifest format, the wire protocol a service speaks, the call interface a plugin uses to reach another plugin, and the usage event. The redux-core lane (job a92d02bf) owns it. Other lanes build against it. Send a change request to `redux-build/core` with `SendMessage`.

Status: draft v1.5. Build against it.

## Plugin folder

A plugin is a folder. Its top level holds one manifest file, `awm-plugin.toml`. The gateway loads a plugin from any plugin root, in the awm repository or outside it. A git install clones the repository at a pinned commit into a root, then loads the folder the same way.

A folder with a `run.sh` and no `awm-plugin.toml` loads as a legacy plugin. The gateway reads it as one plugin with one service, named after the folder. It reads today's `service.toml` keys `tool`, `tier` and `profiles`. So every service under `awm/services/` loads unchanged, and a lane converts one service at a time.

Plugin roots are an ordered list in the gateway config. The built-in core roots are `awm/services` then `awm/plugins`. Put a new core plugin at `awm/plugins/<name>/`. The first root that holds a plugin name wins. The gateway logs and ignores a shadowed folder. Today's Python gateway also scans `awm/plugins` as a built-in root, so a plugin there runs before the cutover.

A tool name on the reserved list of the gateway config belongs only to a plugin from a core root with `trust = "own"`. Any other claim refuses the plugin. The `awm` tool belongs to the core: the gateway serves its verbs itself. A legacy core service that publishes into `awm` today, such as `auth`, adds its verbs to it until it moves to a tool of its own. The default list is `awm`, `2fa`, `approver`, `queue-store` and `journal`, plus the singleton tools of the edge list (§ Install config).

## Manifest

Example, for a plugin that serves one MCP tool, one hidden tool and one page:

> ```
> [plugin]
> name = "journal"
> version = "1.4.0"
> trust = "own"            # own | third-party (R116 item 4)
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
> name = "journal"
> uuid = "6f1c2c1e-0d4b-4f3e-9a51-3b8f2d7a9c10"
> major = 1
> service = "journal"
> tier = "on"              # on: on the MCP surface. off: gateway and CLI only
>
> [[tool]]
> name = "journal-admin"
> uuid = "a0e5b7d2-6c1f-4b8e-8d33-2f9e1c4a7b55"
> major = 1
> service = "journal"
> tier = "off"
>
> [[page]]
> name = "feed"
> dir = "pages/feed/dist" # optional. Absent: found by name in the page roots
> entry = "index.html"
>
> [needs]
> secrets = ["JOURNAL_DB_KEY"]
> network = ["github.com"]
> platform = ["linux"]     # optional. Empty is any
> gpu = false              # optional. Recorded, not enforced yet
>
> [[needs.tool]]
> uuid = "0b7e9a44-3d2c-4c55-b1f0-8e6a2d9c3f21"   # the tool this plugin calls
> major = 2
> ```

Rules the loader enforces:

1. A tool name is letters, digits and hyphens. It holds no dot and no underscore. Use `harness-cc`, never `harness.cc`.
2. A tool name that two plugins claim refuses the second plugin (R116 item 5). Legacy folders are the exception while they convert: several may share a tool, as `cx`, `keep` and `reflection` share `reflection` today. Tony settles the clash by renaming one in the install config.
3. A tool UUID that two plugins serve refuses the second plugin (R117 item 3).
4. A `needs.tool` UUID that no plugin serves, on this node or a peer, refuses the plugin. The refusal names the UUID.
5. A `needs.tool` major that differs from the provider's major refuses the plugin.
6. `tier = "off"` keeps a tool off the MCP surface. Plugins and the CLI still call it through the gateway.
7. Each `[[tool]]` names the services of the same plugin that serve it: `service = "<name>"`, or `services = ["board", "reporting"]` when several share it. A plugin with one service may omit both. A service publishes each function of its `ready` manifest as `<tool>_<verb>`, as today. Two services that publish one verb of a tool refuse the later one at `ready`.
8. A plugin whose `needs.platform` excludes this node's operating system is refused.
9. A `page.dir` or `service.path` that leaves the plugin folder refuses the plugin. A page with no `dir` resolves by name in the gateway's page roots, because the core pages still build to `awm/pages/<name>/dist`.
10. `needs.secrets` and `needs.network` are recorded now. The gateway enforces them in a later change (R116 item 1).

A legacy plugin gets the tool UUID `uuid5(NAMESPACE_URL, "awm:tool:<name>")`. A converted service commits that same UUID into its manifest, so a dependent resolves before and after the conversion alike. Keep it forever. A new tool may use any UUID, such as one from `uuidgen -r`.

## Contracts

An interface that several plugins serve is a contract (`install.md` R117 item 4). Tony: "Option a. Reflection caller doesnt care about details".

1. A contract has a permanent contract UUID and a major version.
2. Each provider keeps its own tool UUID. It names the contracts it implements on its `[[tool]]`: `implements = ["<contract-uuid>@<major>"]`.
3. A dependent names the contract, never a provider: `[[needs.contract]] uuid, major`.
4. The contract resolves to every installed provider, zero or more. A contract UUID never clashes, because many providers are its purpose.
5. `AWM_RESOLVED_TOOLS` lists the providers of each needed contract under `contracts`: `{"<contract-uuid>": [{name, major, plugin, peer}, ...]}`.

Example: cch serves `harness-cc` and dsh serves `harness-ds`. Each implements the `harness` contract at major 1. reflection needs that contract and dispatches on the platform code of the session.

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
3. `rename` maps a declared tool name to its public name, to settle a clash (R116 item 5).
4. `plugin_roots` adds roots after the core roots. `profiles` and `env` pass through to services.
5. `[[external]]` names a service the gateway serves but does not spawn, as `{service, token_file?}`. Another user's systemd unit runs it (R99, R107). See § External services.

The `[fleet]` table holds the rollout order: `home`, `nodes` (starting with `home`), and an optional `[fleet.project.<name>]` with `canonical` and `nodes`. The routine plugin reads it. The gateway ignores it. The routine plugin also honours `AWM_INSTALL_CONFIG` as an override of the path, for tests and dry runs. The gateway reads only `<AWM_DIR>/config.toml`.

The edge list is node config too. The gateway reads `AWM_EDGES_FILE`, else `<AWM_DIR>/edges.yml`. It adds `[s.tool for s in singletons]` to the reserved list. A missing file is an empty fleet.

The loader warns on a `[placement]` table and ignores it.

## Service wire protocol

The protocol is today's Python gateway protocol plus one header, the service token in step 2. `awm.gatewayclient.ServiceAdapter` on `feat/redux-core` speaks it. An older adapter gets 403 from the Rust gateway.

1. The gateway starts each enabled service with `bash <run>` in the service folder. It sets `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_PLUGIN`, `AWM_RESOLVED_TOOLS`, `AWM_NODE_NAME`, `AWM_DIR`, `AWM_WORKSPACE` and `AWM_HUB_TOKEN_FILE` in the environment, plus the install config's `env`. It never sets `AWM_SERVICE_ID`.
2. The service posts `POST /hub/service/register` with `{name, prefix?, pid, start, cwd}` and the header `X-Awm-Service-Token`, read from `AWM_HUB_TOKEN_FILE`. Each spawn gets a fresh token, so no other local user can take the service's name while it is down. A register with a wrong token gets 403. The reply carries `service_id`, `control_ws_path` and `canonical_workspace`.
3. The service opens the control WebSocket at `/hub/service/control/<service_id>`. It sends `{"kind":"ready","api":{"functions":[...]}}` first. Each function is `{name, description, params:[{name,type,required,description}], tool?}`.
4. The gateway sends `{"kind":"call","id","fn","args","as"?,"caller"}`. `caller` is `{"node","lane","session","trust"}`: `lane` is `local`, `edge` or `service`, `session` is the R9 session id or null, and `trust` is `trusted`, `cooperative` or `untrusted`. Only the gateway fills `caller` (R103). It drops any `X-Awm-Trust` or `X-Awm-Lane` header a caller sends. A service that predates `caller` ignores it. The service answers `{"kind":"reply","id","ok":true,"result"}` or `{"kind":"reply","id","ok":false,"error","error_class"}`. The gateway may also send `notify`, `cancel` and `shutdown`.
5. The service may send `{"kind":"emit","topic","payload"}` and `{"kind":"usage",...}` (see § Usage event).
6. A closed control WebSocket releases the service. The gateway respawns it after a 10 s window unless the gateway is stopping.

A second instance of a live service gets 409 on register and stands down.

### External services

A service in the install config's `[[external]]` list runs under its own system user, apart from the gateway's user. The gateway never spawns it. It waits for the service to register, then serves it like any other.

1. Run the service's `run.sh` from its unit, with `WorkingDirectory` set to the service folder. Set `AWM_HUB_URL` to the gateway's URL and `AWM_SERVICE_NAME` to the service name.
2. Set `AWM_HUB_TOKEN_FILE` to a file that holds the service's token.
3. Point the entry's `token_file` at a file with the same token, readable by the gateway's user.
4. Read resolved tools from `GET /hub/plugins/<plugin>/resolved`. The service cannot read `AWM_RESOLVED_TOOLS` in the gateway's state.

## Calling another plugin

A plugin calls another plugin only through the gateway (R117 item 1).

1. Read the current name of each tool you need from the JSON file at `AWM_RESOLVED_TOOLS`. Its `tools` key maps each `needs.tool` UUID to `{name, major, plugin, peer}`. Its `contracts` key lists the providers of each `needs.contract` UUID. The gateway rewrites it when a rename lands, then restarts the dependent.
2. Call `POST $AWM_HUB_URL/invoke` with `{"name":"<tool>","args":{"verb":"<verb>","args":{...},"peer":"<node>"?}}`.
3. Send `X-Awm-Plugin: <your plugin name>`. The gateway counts the call as surface `plugin` for both plugins.
4. Send `X-Awm-As: <principal>` when you act for someone, as today.
5. Read `{"result": <string>}` on success. The string is the verb's JSON result. An error is a non-2xx status with `{"detail": ...}`.

`GET /hub/resolve/<uuid>` answers the same record as the file, for a caller with no manifest. The old door `POST /svc/<service>/fn/<fn>` stays for today's Python services and is removed once they move.

A call to a tool that is not up yet waits up to 10 s, then fails with 503 and names the tool (R117 item 6).

## MCP and CLI surface

The MCP surface is today's surface. Each tool is one MCP tool with input `{verb, args, peer}`. `verb = "describe"` returns the verb schemas. `peer` names the node to run on. The MCP proxy (`awm mcp`) reads `GET /tools?view=domains` and posts `POST /invoke` with `X-Awm-Surface: mcp`. The CLI (`awm <tool> <verb> [--key value]...`) posts the same call with `X-Awm-Surface: cli`.

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
| `mcp`, `cli`, `http`, `plugin` | the gateway, on each `/invoke`, from `X-Awm-Surface` and `X-Awm-Plugin` |
| `page` | the gateway, on each HTML document GET under `/p/<plugin>/<page>/` |
| `background` | the plugin, as a control-WS frame `{"kind":"usage","surface":"background","name":"<job>","ok":true,"ms":840}` |
| `code` | the library, as `POST /usage` with the event body, for use straight from another project's code |
| `page` on its own origin | the plugin, as `POST /usage` with `surface = "page"` and the page `name`, for a page the gateway does not serve, such as the approver panel |

The Python helpers are `awm.usage.record_background(job, ok, ms)` and `awm.usage.record_code(plugin, name)`, in `awm/service_components/usage` (redux-plugins lane). Until the Rust gateway cuts over, the Python gateway sinks these two kinds into the same files.

The gateway fills `v`, `ts` and, for a control-WS frame, `plugin`. It appends each event to `<AWM_DIR>/usage/<YYYY-MM-DD>.jsonl` and keeps a 60-day tally per plugin and surface. `GET /usage` and `awm usage` read the tally.
