# Installing the `penpot-view` service

## Purpose & Contents

How to install, configure, run, and verify `penpot-view`: a Python-only awm
service with no static web-client tree and no per-user scaffolding. Covers the
service-account credentials it needs, the two gateway registrations it holds,
the env vars that change its behaviour, and the local-stack ports it depends
on. Anything the code already shows (verb signatures, cache internals) belongs
in the module docstrings, not here — see `awm/penpot_view/view.py` and
`awm/penpot_view/renderspec.py` for those.

## What problem it solves

Penpot has no URL that returns a board as a live image the way drawio's own
page-view mount does. `penpot-view` adds one:
`GET /penpot-view/<file-id>/<page-id>/<board-id>` returns that board's current
render as `image/svg+xml`, cached and kept fresh — placeable into another
diagram, an autopublish link, a doc, anywhere a stable image URL is useful.

## The contract

- **Does not touch `userroot` or any per-user scaffolding.** Every render is
  keyed on Penpot's own `file-id`/`page-id`/`board-id`, resolved against one
  shared service-account session — there is no notion of "whose" board a
  render belongs to, so nothing here reads or writes a user's worktree.
- **Ships no static web-client tree.** Unlike `drawio`, which serves the
  editor's ~150 MB of JS/CSS/images at its own `kind=static` mount,
  `penpot-view` has nothing for a browser to load directly — the one thing it
  serves is the render URL itself.
- Query parameters `scale`, `swap=<from>:<to>` (repeatable), and `crop=<name>`
  choose a variant of the same board. The grammar is
  `awm.penpot_view.renderspec`'s.
- Three service verbs beyond the render URL: `status`, `force_refresh`,
  `cache_stats` — see `awm penpot-view status` etc. under *Verify*.

## Registrations

Two, both named `penpot-view` so an operator listing registrations sees one
service under one name — unlike drawio, there is no second mount competing
for the plain name.

| kind | name | prefix | what |
|---|---|---|---|
| `service` | `penpot-view` | `/svc/penpot-view` | the three verbs, plus supervision |
| `url` | `penpot-view` | `/penpot-view` | the loopback listener that renders a board |

The control WS does not cover mounts, so the `url` mount runs its own
register/hold-lease/reconnect loop (`awm.penpot_view.view.ViewServer.
hold_mount`, composed by `awm.penpot_view.mount`) — the registry is in-memory,
and without that loop the mount would 404 after any gateway restart.

**CAUTION** The gateway forwards the *full* path to a `kind=url` mount without
`strip_prefix` — it does not remove `/penpot-view` before proxying. The
listener parses relative to the whole prefix for exactly that reason. Pointing
a future `strip_prefix: true` registration at this mount would make every
request 404, since the handler would see a path with the segment it expects
to strip already gone.

## Running it against the local stack

The local Penpot stack is docker compose project `penpot-local`. Its frontend
is the one origin this service ever talks to.

**One base URL for the whole round trip, not two.** An export is two HTTP
hops — `POST` the exporter to render, then `GET` the resulting asset — and
both must go through Penpot's frontend nginx, never the backend or the
exporter container directly:

- The exporter container publishes **no host port**. It listens on `6061`
  inside the compose network only, so `http://localhost:6061` is unreachable
  from the host. That address only ever works for a process running inside
  the same compose network. The frontend proxies `/api/export` to it, which is
  why `PENPOT_EXPORTER_URL` defaults to the frontend's own origin, not `6061`.
- The asset fetch that follows a render **must** go through the frontend too.
  The backend's own object handler answers a direct asset request with
  `HTTP 204` and an `x-accel-redirect` header meant for an nginx `internal`
  location it does not itself have — zero bytes, no error, and a blank SVG
  that looks like a successful export. Two rules in `exporter_client.py` keep
  that from being reachable, and both matter. It **rebases** every accepted URL
  onto `PENPOT_BASE_URL` before a socket opens, so whatever origin Penpot
  stamped on the URL, the request goes to the frontend. And it accepts a URL
  only if its origin is one it recognises *and* its path is on the caller's
  allow-list. Do not add an option that skips either.

Configure the service account and, if the compose stack's default port
differs, the frontend origin:

| Var | Default | Purpose |
|---|---|---|
| `PENPOT_BASE_URL` | `http://localhost:9001` | frontend origin for login and the asset fetch. Every accepted URL is rebased onto this before a request leaves. |
| `PENPOT_EXPORTER_URL` | `http://localhost:9001` | frontend origin for the export POST (same origin as above — differs only if your stack proxies exports elsewhere) |
| `PENPOT_PUBLIC_URI` | *(unset)* | the origin, with its mount path, that Penpot stamps on browser-bound URLs. Recognised, then stripped of its mount and rebased. |
| `PENPOT_INTERNAL_URI` | *(unset)* | the origin the exporter's own browser rendered against, when that differs from the public one — see below |
| `PENPOT_SERVICE_USERNAME` | *(unset)* | login for the service account `export_svg`/`file_etag` authenticate as |
| `PENPOT_SERVICE_PASSWORD` | *(unset)* | — |

Without a username and password configured, every render fails at the first
export with a named `ExporterError` rather than a silent blank image — check
`awm penpot-view status`'s `service_account_configured` field first when
nothing renders.

**`PENPOT_INTERNAL_URI` is a recognition token, never a routing target.** Set
it where the exporter is pointed at an origin only the compose network can
resolve, which is how a stack whose public origin sits behind a sign-in page
renders at all. The render then comes back with that hostname baked into every
image and font URL, and penpot-view has to know the name to accept the URL —
but it never resolves it, because it rebases onto `PENPOT_BASE_URL` first. It
must match the exporter's own origin byte for byte; `awm penpot-view status`
reports both so the comparison is one command. Unset, a render against such a
stack is served with its images and fonts missing, 200, with only an
`X-Penpot-Problems` header to say so.

**CAUTION** on any host where the port matters — sirius, or any machine
reachable beyond loopback — publish the frontend as `127.0.0.1:9001:8080`, not
a bare `9001:8080` (which docker-compose expands to `0.0.0.0:9001->8080`,
listening on every interface). On sirius specifically, docker's own nat-table
rules bypass UFW, so a bare port mapping is a public exposure regardless of
the host firewall's rules. `penpot-view`'s own listener already binds
`127.0.0.1` only, on an ephemeral port, gateway-fronted. This caution is about
the Penpot stack it talks to, not about this service's own port.

## Install

    bash install.sh

Editable-installs `config`, `gatewayclient`, and this service into the `awm`
env (override with `AWM_ENV=<name>`), and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` so the gateway can respawn the service under
systemd's minimal PATH. There is no web client to clone or build and no
container image to pull — the whole install is that one step.

## Env overrides

Beyond the two above:

| Var | Default | Effect |
|---|---|---|
| `PENPOT_VIEW_PREFIX` | `/penpot-view` | origin path for the render URL |
| `PENPOT_VIEW_MOUNT_NAME` | `penpot-view` | the mount's registration name |
| `PENPOT_VIEW_TTL` | `20` | seconds a cached render is served before the next request pays one freshness probe |
| `PENPOT_VIEW_COLD_TIMEOUT` | `120` | how long a request with nothing cached yet will block for the first render |
| `AWM_PENPOT_VIEW_CACHE` | `<AWM_DIR>/services/penpot-view/viewcache` | the durability copy of rendered SVGs |
| `PENPOT_EXPORTER_EXPORT_PATH` | `/api/export` | path the export POST is sent to, on `PENPOT_EXPORTER_URL` |
| `PENPOT_LOGIN_TIMEOUT` / `PENPOT_EXPORT_TIMEOUT` / `PENPOT_ASSET_TIMEOUT` / `PENPOT_FRESHNESS_TIMEOUT` | `15` / `60` / `30` / `10` | per-hop HTTP timeouts, in seconds |

## Scope & caveats

- **No auth**, like every awm service. Anything that can reach the gateway can
  render any board the service account can see.
- **Freshness is per file, not per board** — Penpot exposes no finer-grained
  change tag, so editing any board in a file marks every cached board of that
  file for re-render on its next request, even ones nobody touched.
- **`force_refresh` reaches past `Cache`'s public surface** to evict one slot,
  because `Cache` (in `view.py`) exposes no invalidation method. Worth
  revisiting as a real `Cache.invalidate(key)` if `view.py` grows one.

## Verify

    awm services list                  # penpot-view → running
    awm penpot-view status             # service account configured?, mount up?, cache dir
    curl -s -o /dev/null -w '%{http_code}\n' \
      "$AWM_HUB_URL/penpot-view/<file-id>/<page-id>/<board-id>"
    awm penpot-view cache_stats        # renders_total should be nonzero after the curl above
    awm penpot-view force_refresh --file-id <file-id> --page-id <page-id> --board-id <board-id>
