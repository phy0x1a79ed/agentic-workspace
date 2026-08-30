# Installing the `penpot-plugins` service

## Purpose & Contents

This service hosts every Penpot plugin awm authors itself. It gives each one
a stable install URL and holds a copyable scaffold for the next one. This
file covers what the service does, why it lives outside the Penpot fork, how
to install and verify it, and how to add a new plugin. Plugin API usage
itself belongs in each plugin's own source (`local/<name>/plugin.js` or the
template's `plugin.ts`), not here.

## What problem it solves

Penpot's Plugin Manager installs a plugin from any URL that serves a
`manifest.json` — ConnectFlow, for example, is hosted on Cloudflare Pages, a
third-party static host outside our control. A plugin we author ourselves
needs the same kind of URL, but pointed at infrastructure we run: `awm`'s own
gateway, the same way `drawio` hosts its web client and `fileviewer` hosts
arbitrary files, both as a `kind=static` mount.

## Why this tree lives in the `awm` repo, not the Penpot fork

The Penpot fork (`projects/penpot/<scope>/`) has its own `plugins/` directory
— an upstream pnpm workspace of Penpot's own example plugins. Putting our
plugins there would collide with that workspace on a future Penpot version
bump: a merge from upstream could silently overwrite or restructure
first-party plugin code sitting inside a directory upstream considers its
own. Keeping `local/` here, in a completely separate repo, means an upstream
merge can never touch it.

## How it works

`awm/penpot_plugins/mount.py` registers a `kind=static` mount at
`/penpot-plugins`, rooted at `local/` beside this file, and holds its lease
for the process lifetime — the same register/hold-lease/reconnect loop
`drawio` and `fileviewer` use for their own static mounts, because the
control WS does not cover mounts and the in-memory registry drops them on
every gateway restart. `awm/penpot_plugins/hub_adapter.py` is the
`kind=service` registration at `/svc/penpot-plugins`: it buys supervision (so
the gateway respawns the process) and one verb, `status`, which reports the
mount's live state and which plugin folders it found.

A folder under `local/` is a plugin if it has a `manifest.json` and does not
start with `_` (`_template` is deliberately excluded from that list — it is
a scaffold, not an installable plugin). Its install URL is:

    /penpot-plugins/<name>/manifest.json

Paste that into Penpot's in-app Plugin Manager (`Ctrl+Alt+P`, or Menu ▸
Plugins ▸ Plugins manager). Locally, against the compose stack at
`http://localhost:9001`, that is:

    http://localhost:9001  (Plugin Manager's own UI)
    → manifest URL: http://<awm gateway host>:<port>/penpot-plugins/penpot-view-refresh/manifest.json

The gateway and the Penpot stack are two separate local processes today —
the manifest URL has to be reachable from wherever Penpot's frontend and
backend actually run, which is why every URL a plugin stores (see
`penpot-view-refresh` below) must be absolute, not origin-relative. Once
`awm/services/penpot/` and httpsfront's `/penpot` wiring land (a sibling
task in this same plan), both sides sit behind one edge and this note
simplifies to "the gateway's own origin."

## Adding a new plugin

1. Copy `local/_template/` to `local/<your-plugin-name>/`.
2. Edit `manifest.json`: name, description, permissions.
3. Edit `plugin.ts`, following its own comments and the two files it points
   at (Penpot's `docs/plugins/create-a-plugin.md` and
   `plugins/libs/plugin-types/index.d.ts`, both in the Penpot fork).
4. Build `plugin.ts` to `plugin.js` (the template's last comment block shows
   the one-line `esbuild` invocation) and drop the output beside
   `manifest.json`.
5. Add an `icon.png` (any image format works; Penpot recommends 56×56).
6. Restart or wait for the running `penpot-plugins` service to notice — no
   restart is actually required, since the mount reads `local/` off disk on
   every request; only a *new* plugin folder needs the service already
   running to be servable at all (the mount registers `local/` as a whole
   directory once, at process start).
7. Paste `/penpot-plugins/<your-plugin-name>/manifest.json` (as an absolute
   URL) into Penpot's Plugin Manager.

## The `penpot-view-refresh` plugin

`local/penpot-view-refresh/` is the first occupant of this mount. It links a
shape in a Penpot file to a `/penpot-view/<file>/<page>/<board>` render URL
(the companion `penpot-view` service's live-SVG endpoint) via per-shape
plugin data, and on a manual "Refresh imports" click in its UI panel,
re-fetches that URL through `penpot.uploadMediaUrl()` and reassigns the
shape's fill image to the result.

The fetch itself happens in Penpot's own backend, not in the plugin sandbox
— see `penpot.uploadMediaUrl`'s doc comment in `plugin-types/index.d.ts` —
so there is no CORS concern and the plugin never sees the image bytes
directly. **WARNING: that is also why Refresh cannot work behind the awm
edge as this plugin stands. See "Three things the fetch requires" below
before you deploy it.**

**Manual only, by design for now.** The plugin ships with no automatic
refresh — no on-focus listener, no polling interval. The Penpot plugin API
exposes no "window focus" event to the sandboxed plugin script itself (only
`pagechange`/`shapechange`/`selectionchange`/`themechange`/`filechange`/
`contentsave`/`finish` — see `penpot.on`'s doc comment); wiring on-focus
refresh would mean listening in the UI iframe's own `window` and relaying a
message back, which is deferred until manual refresh has been verified
against a real running instance. A naive `setInterval` was avoided on
purpose too — browsers throttle timers in a backgrounded tab, so a refresh
that silently stops firing there would be worse than one that never
existed.

**Fidelity through Penpot's ingestion: verified, and it stays vector.** A
real `penpot-view` render imported through the same RPC `uploadMediaUrl`
uses comes back `image/svg+xml` at its original 1390x724, with all 197
shape groups and all ten inlined `data:` sub-resources intact. Penpot
re-serialises the document (single-quoted attributes, an added XML
declaration) but does not rasterize or strip it.

**Three things the fetch requires. The first two have answers. The third
does not, and it is what stops Refresh from working today.**

1. **Penpot's backend does the fetching, so the render URL must be
   reachable from inside that container.** The awm gateway binds loopback
   only, so a `/penpot-view/...` URL on `127.0.0.1` is unreachable from
   Penpot's containers — confirmed from inside `penpot-backend`, via both
   its own localhost and the compose bridge gateway.

2. **Penpot refuses private-network targets outright.** Its SSRF guard
   (`backend/src/app/util/ssrf.clj`) rejects loopback, link-local,
   site-local and RFC1918 addresses with `:ssrf-blocked-target`, so even a
   reachable private address is refused. The supported escape hatch is
   `PENPOT_SSRF_ALLOWED_HOSTS`, an exact, case-insensitive host allow-list
   that bypasses the IP check — name the host serving `/penpot-view` there
   rather than widening the guard or making the render public.

3. **The backend holds no awm session, so the edge answers it 401.**
   `/penpot-view/...` sits behind the edge's auth gate. The gate reads a
   session cookie or an `Authorization` bearer. `uploadMediaUrl` takes a URL
   and nothing else, so the backend's fetch carries neither. Every Refresh
   click therefore fails, on any deployment where the render endpoint is
   gated — which is every deployment, because ungating it would publish
   every board.

   The plugin sandbox cannot fetch it either, so "have the plugin do it
   instead" is not a one-line change: `createSandbox` in
   `plugins/libs/plugins-runtime/src/lib/create-sandbox.ts` forces
   `credentials: 'omit'` and blanks `Authorization` on the `fetch` it
   exposes, and its wrapped response object offers only `text`/`json` — no
   `blob`, no `arrayBuffer`.

   Two routes remain open. Neither is built, and the choice between them
   changes what the feature is:

   - **Fetch in the UI iframe, upload bytes.** The panel iframe runs with
     `allow-same-origin` (see `modal/plugin-modal.ts`) and is served from
     this mount on the edge's own origin, so a `fetch` there does carry the
     awm session cookie. It would hand the bytes to `plugin.js` for
     `penpot.uploadMediaData(name, bytes, mimeType)`. **CAUTION: that call
     routes an `image/svg+xml` blob into Penpot's SVG-to-shapes importer
     (`process-blobs` in `frontend/src/app/main/data/workspace/media.cljs`),
     not into media storage, so it yields shapes rather than a fill image.**
     Keeping the fill-image model means rasterizing the SVG in the iframe
     first, which gives up the vector fidelity recorded above.

   - **Let the edge accept a scoped token in the query string.** Render
     params already ride in the query string, so a signed, expiring,
     render-scoped token fits the existing URL shape and keeps
     `uploadMediaUrl` and the vector path intact. This is an edge auth
     change, not a plugin change.

**Storage format.** The source URL is stored under the plugin-data key
`penpot-view-refresh:sourceUrl`, one per shape. Linking again overwrites it;
nothing else reads or writes that key.

## Install

    bash install.sh

Editable-installs `config` and `gatewayclient` (this service's only two
component-lib dependencies — there is no database and no third-party
package) into the `awm` env (override with `AWM_ENV=<name>`), and writes a
gitignored `.runtime-env` sidecar baking `AWM_PYTHON` so the gateway can
respawn the service under systemd's minimal PATH.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `PENPOT_PLUGINS_MOUNT_PREFIX` | `/penpot-plugins` | origin path for the mount |
| `PENPOT_PLUGINS_ROOT` | `<service>/local` | filesystem root the mount serves |

## Verify

    awm services list                       # penpot-plugins → running
    awm penpot-plugins status               # mount prefix, root, plugin list, lease state

    curl -s http://127.0.0.1:7819/penpot-plugins/penpot-view-refresh/manifest.json
    curl -s http://127.0.0.1:7819/penpot-plugins/penpot-view-refresh/plugin.js | head -c 80

Then, against a running Penpot instance: open the Plugin Manager, paste the
manifest URL above (substituting whatever host/port actually fronts the
gateway), install, select a shape, and link a real `/penpot-view/...` URL to
it. Refresh imports fails there for the reason in item 3 above. Do not read
that failure as a broken install.

**What was and was not verified.** The mount, manifest shape, and plugin
logic were checked structurally — against Penpot's own plugin docs and
`plugin-types/index.d.ts`, and with `pytest` against this service's own test
file. Refresh was traced through all three of its candidate fetch paths
(backend RPC, plugin sandbox, UI iframe) in Penpot's own source and shown to
have no working one. Nothing here was driven in a browser: whether install
and link behave as written is still unconfirmed, and confirming them buys
little while Refresh has no route.
