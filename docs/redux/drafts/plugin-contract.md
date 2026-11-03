# Plugin contract, draft v1

## Purpose & Contents

This file is the contract between a plugin and the Rust gateway of the redux (`install.md` R112, R115 to R117, R132). It holds the manifest format, the wire protocol a service speaks, the call interface a plugin uses to reach another plugin, and the usage event. The redux-core lane (job a92d02bf) owns it. Other lanes build against it. Send a change request to job a92d02bf with `reflection(verb="message")`.

Status: draft. Items marked **open** wait on the coordinator (job 8162a704). Build against the rest.

## Plugin folder

A plugin is a folder. Its top level holds one manifest file, `awm-plugin.toml`. The gateway loads a plugin from any plugin root, in the awm repository or outside it. A git install clones the repository at a pinned commit into a root, then loads the folder the same way.

A folder with a `run.sh` and no `awm-plugin.toml` loads as a legacy plugin. The gateway reads it as one plugin with one service, named after the folder. It reads today's `service.toml` keys `tool`, `tier` and `profiles`. So every service under `awm/services/` loads unchanged, and a lane converts one service at a time.

Plugin roots are an ordered list in the gateway config. The built-in core roots are `awm/services` then `awm/plugins`. Put a new core plugin at `awm/plugins/<name>/`. The first root that holds a plugin name wins. The gateway logs and ignores a shadowed folder. Today's Python gateway also scans `awm/plugins` as a built-in root, so a plugin there runs before the cutover.

A tool name on the reserved list of the gateway config belongs only to a plugin from a core root with `trust = "own"`. Any other claim refuses the plugin. The default list is `2fa`, `approver`, `queue-store` and `journal`, plus the singleton door tools that `fleet/edges.yml` names under `singletons`.

## Manifest

Example, for a plugin that serves one MCP tool, one hidden tool and one page:

> ```
> [plugin]
> name = "journal"
> version = "1.4.0"
> trust = "own"            # own | third-party (R116 item 4)
> refresh = "./refresh.sh" # optional R33 hook, run in the plugin folder
>
> [placement]
> nodes = ["*"]            # default. "*" is every node
> dev_nodes = []           # optional. Nodes that keep it for development
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
>
> [[needs.tool]]
> uuid = "0b7e9a44-3d2c-4c55-b1f0-8e6a2d9c3f21"   # the tool this plugin calls
> major = 2
> ```

Rules the loader enforces:

1. A tool name is letters, digits and hyphens. It holds no dot and no underscore. Use `harness-cc`, never `harness.cc`.
2. A tool name that two plugins claim refuses the second plugin (R116 item 5). Tony settles the clash by renaming one in the install config.
3. A tool UUID that two plugins serve refuses the second plugin (R117 item 3).
4. A `needs.tool` UUID that no plugin serves, on this node or a peer, refuses the plugin. The refusal names the UUID.
5. A `needs.tool` major that differs from the provider's major refuses the plugin.
6. `tier = "off"` keeps a tool off the MCP surface. Plugins and the CLI still call it through the gateway.
7. Each `[[tool]]` names one `[[service]]` of the same plugin. A service publishes each function of its `ready` manifest as `<tool>_<verb>`, as today. A service with one tool may omit `service` on the tool.
8. A plugin whose `placement.nodes` and `placement.dev_nodes` both exclude this node is skipped. The node name is `AWM_NODE_NAME`, else the hostname.
9. A `page.dir` or `service.path` that leaves the plugin folder refuses the plugin. A page with no `dir` resolves by name in the gateway's page roots, because the core pages still build to `awm/pages/<name>/dist`.
10. `needs.secrets` and `needs.network` are recorded now. The gateway enforces them in a later change (R116 item 1).

A legacy plugin gets the tool UUID `uuid5(NAMESPACE_URL, "awm:tool:<name>")`. A converted service commits that same UUID into its manifest, so a dependent resolves before and after the conversion alike. Keep it forever. A new tool may use any UUID, such as one from `uuidgen -r`.

A `[[needs.tool]]` entry may carry `optional = true`. A missing optional tool does not refuse the plugin. Its entry in `AWM_RESOLVED_TOOLS` is absent, and the dependent checks for it before a call. Use it for a dependent that calls whichever of several providers is installed, such as reflection calling the harness plugins cch (`harness-cc`) and dsh (`harness-ds`).

**open** — a contract that several plugins serve. Until Tony decides, build to option (b): the dependent lists each provider's tool UUID in `[[needs.tool]]` with `optional = true`. Option (a), written up for a small switch: each provider keeps its own tool UUID and adds `implements = ["<contract-uuid>@1"]` to its `[[tool]]`. A dependent names `[[needs.contract]] uuid, major` and resolves to every provider, zero or more. A contract UUID never clashes, because many providers are its purpose. The loader accepts both forms.

## Refresh hook

The deploy runs a plugin's `refresh` command when the files of the plugin folder changed between two releases (`routines.md` R33).

1. The deploy runs the command in the plugin folder.
2. It sets `AWM_REFRESH_FROM` to the old release commit, `AWM_REFRESH_TO` to the new one, and `AWM_NODE_NAME`.
3. Exit 0 means refreshed. A nonzero exit is a failed refresh, and the deploy dispatches a fixer.
4. A changed plugin with no `refresh` key falls back to the project hook, `deploy.toml` `[refresh]`.

## Service wire protocol

The protocol is today's Python gateway protocol, unchanged. `awm.gatewayclient.ServiceAdapter` already speaks it.

1. The gateway starts each enabled service with `bash <run>` in the service folder. It sets `AWM_HUB_URL`, `AWM_SERVICE_NAME`, `AWM_SERVICE_ID`, `AWM_PLUGIN` and `AWM_RESOLVED_TOOLS` in the environment.
2. The service posts `POST /hub/service/register` with `{name, prefix?, pid, start, cwd}`. The reply carries `service_id`, `control_ws_path` and `canonical_workspace`.
3. The service opens the control WebSocket at `/hub/service/control/<service_id>`. It sends `{"kind":"ready","api":{"functions":[...]}}` first. Each function is `{name, description, params:[{name,type,required,description}], tool?}`.
4. The gateway sends `{"kind":"call","id","fn","args","as"?,"caller"}`. `caller` is `{"node","lane","session","trust"}`: `lane` is `local`, `edge` or `service`, `session` is the R9 session id or null, and `trust` is `trusted`, `cooperative` or `untrusted`. Only the gateway fills `caller` (R103). It drops any `X-Awm-Trust` or `X-Awm-Lane` header a caller sends. A service that predates `caller` ignores it. The service answers `{"kind":"reply","id","ok":true,"result"}` or `{"kind":"reply","id","ok":false,"error","error_class"}`. The gateway may also send `notify`, `cancel` and `shutdown`.
5. The service may send `{"kind":"emit","topic","payload"}` and `{"kind":"usage",...}` (see § Usage event).
6. A closed control WebSocket releases the service. The gateway respawns it after a 10 s window unless the gateway is stopping.

A second instance of a live service gets 409 on register and stands down.

## Calling another plugin

A plugin calls another plugin only through the gateway (R117 item 1).

1. Read the current name of each tool you need from the JSON file at `AWM_RESOLVED_TOOLS`. It maps each `needs.tool` UUID to `{name, major, plugin, peer}`. The gateway rewrites it when a rename lands, then restarts the dependent.
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
