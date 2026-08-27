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
directly.

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

**Known, unverified risk: image fidelity through Penpot's own ingestion.**
`uploadMediaUrl` hands the fetched bytes to Penpot's own media pipeline,
which may rasterize or re-encode an SVG on the way in — this plugin has not
been driven against a live Penpot + `penpot-view` instance to confirm
whether a refreshed board still renders as crisp vector output or comes back
rasterized. Verify this before relying on it for anything where fidelity
matters, and update this paragraph with the answer once it is checked.

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
gateway), install, select a shape, link a real `/penpot-view/...` URL to it,
and click Refresh imports. Confirm the shape's fill updates and check the
fidelity question above while you're there.

**What was and was not verified in this pass.** The mount, manifest shape,
and plugin logic were checked structurally — against Penpot's own plugin
docs and `plugin-types/index.d.ts`, and with `pytest` against this service's
own test file — but not driven end-to-end in a browser against a live
Penpot instance. Treat "install works, link works, refresh works, image
looks right" as unconfirmed until someone does that pass.
