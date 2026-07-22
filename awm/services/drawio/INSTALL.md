# Installing the `drawio` service

A Python feature service in the `awm.drawio` namespace, plus a static mount for
the drawio web client and a page at `/ui/drawio`. It gives agents and people a
way to edit the same diagram concurrently without silently losing each other's
work.

## What problem it solves

Diagram authoring previously ran out of `projects/drawio/biomass-map-smoke`: a
bespoke loopback server, a patched drawio client polling and PUTting the whole
file every two seconds, and project scripts writing the same file behind the
server's back. Two writers, one file, no protocol. That cost real work — a tab
left open across a scripted rebuild autosaved its stale in-memory model back
and took one page from 47 cells to 2, and the directory accumulated roughly
fifty hand-named `.bak` files as its only mitigation.

## The contract

**Editing is operations; merging is git.**

Operations are safe *inside* a checkout because it has exactly one writer.
They are not safe *across* writers: merging two operation streams yields a
structurally valid document that can be semantically wrong with **no conflict
raised** — an agent shifting a row of cells to make room, against a person
dragging one of those same cells, merges cleanly attribute-by-attribute and
leaves the row visibly broken. A merge algorithm that cannot tell you it failed
is not a merge algorithm. So the merge boundary uses git's line-based three-way
merge, which is dumber and fails loudly.

    drawio checkout <save>        →  handle
    drawio edit <handle> --ops …     apply operations (all-or-nothing)
    drawio path <handle>             the file, for looking at or hand-editing
    drawio url --handle <handle>     the editor, for looking at the render
    drawio status <handle>           ahead / behind / conflicted
    drawio update <handle>           pull live changes in — the ONLY place
                                     reconciliation happens
    drawio resolve <handle>          declare a hand-resolved checkout clean
    drawio merge <handle>            land it
    drawio discard <handle>

`merge` **is never actually a merge.** It refuses while the checkout is behind,
so landing is a guarded single-file write: atomic, and incapable of producing a
document neither side asked for. All reconciliation happens in `update`, inside
the agent's own checkout, at a moment it chooses, where it can render the result
and check it.

### The escape hatch

`update` conflicts land in the checkout as ordinary `<<<<<<<` / `=======` /
`>>>>>>>` markers. Edit the file at `drawio path <handle>` by hand, then call
`drawio resolve`. `merge` refuses while markers remain, and `resolve` refuses
if the file no longer parses as a diagram.

This is deliberate. v1 will meet edge cases the operation layer cannot express,
and hitting one should cost an afternoon of manual editing — not a blocked
workflow.

### Landing against a live editor tab

`merge` tells open tabs to flush and hold, confirms the tip has not moved,
lands, releases, and pushes the result back. Without the flush, an in-flight
autosave carrying a pre-merge snapshot could land immediately after and revert
everything. The hold is sub-second and tabs stay editable throughout — only the
*save* is deferred.

Every editor save carries the revision it is based on, so a stale tab is
rejected rather than applied. That makes the prototype's costliest failure
unrepresentable rather than merely unlikely, and retires both heuristic
size/cell-count guards the old server needed.

### Id discipline

Agents own an id namespace prefix (`mol/…`, `axes/…`) and never renumber.
Operations are idempotent by cell id, so re-running a build updates instead of
duplicating — but only if ids are **deterministic**. A re-run that mints new ids
reads as delete-plus-create and the diff stops being reviewable.

## How it works

### The store

One git repository at `<AWM_DIR>/services/drawio/diagrams`, whose working tree
*is* the folder structure the reception page shows. Every accepted write is
normalized and committed; revisions are commits, history is the log, restore is
forward-only.

Diagrams stay independent inside that shared repo because every drift question
is asked **per path** (`git log <base>..HEAD -- <path>`), never repo-wide.

Conflict resolution uses `git merge-file`, which needs no index, no worktree,
and cannot leave `MERGE_HEAD` behind. (Git here is 2.34.1, which predates
`merge-tree --write-tree` anyway.)

### Normalization

Canonical serialization is the property everything else rests on: git merges
text, so any spelling difference between the browser's serializer and the
script layer's is a phantom conflict. Neutralized: `mxGraphModel/@dx,@dy`
(viewport — changes on any scroll, and sits on the line that opens each page's
content), float noise (the real file contains `329.9999999999999`), attribute
order, and `mxfile/@host,@agent,@version`.

**Sibling order is deliberately not normalized** — it is z-order, and sorting it
would be a silent render change. Compressed diagrams are refused outright.

Measured on the real 5.8 MB `prokaryotic_metabolism.drawio`: normalize twice
equals normalize once; browser output and ElementTree output converge
byte-for-byte; a scroll produces zero diff; a one-cell edit produces one hunk.

Consequence worth knowing: a tab that is merely scrolled or page-switched
re-saves to identical canonical bytes, so **scrolling creates no revision**.
Editing bursts by one author fold into a single revision, except onto a
revision a live checkout has pinned.

### Images

Cells reference ordinary files through fileviewer's mount
(`style="shape=image;image=/files/abs/path.svg;"`), so re-rendering a figure
updates the diagram on reload instead of requiring a re-import.

Three hazards this creates, and what handles each:

- **The semicolon landmine.** drawio splits style strings on `;`, so a
  conventional `data:image/svg+xml;base64,…` URI truncates and the cell renders
  blank. A filesystem path has no `;` — which is the whole reason references
  beat embedding. Where export *must* inline (below), it uses the comma form
  with percent-encoded content and an empty safe-set, so a `;` inside the
  payload is escaped too.
- **Silent 404s.** fileviewer's mask is a denylist, and a masked path returns
  exactly the same "not found" as a missing one — so an image stored under a
  masked directory is invisible with nothing logged anywhere. `drawio check`
  reports both cases separately, and `export` refuses by default when any
  reference is broken.
- **Pointing at the wrong copy.** `drawio externalize` matches by content hash
  and takes the *first* root that contains a match, so **root order is
  precedence**. An archive directory typically holds byte-identical copies of
  what the renderer currently emits, so a reference into it resolves, renders
  identically, and passes `check` — and then silently never updates again,
  which forfeits the only reason to externalize. Name the live render
  directories explicitly rather than one parent that sweeps up its own archive
  subdirectories. Nothing detects this for you; the failure is a figure that
  quietly stops tracking its source.

### Export

`drawio export` renders through the `jgraph/export-server` container. Images are
inlined **server-side before the document is handed over**, so the container
needs no network at all and the output is self-contained by construction — no
host routing, and nothing to break when the gateway is down.

### Registrations

Three, all named `drawio` (the registry keys records on `(kind, name)`):

| kind | prefix | what |
|---|---|---|
| `service` | `/svc/drawio` | the verbs, plus supervision |
| `static` | `/drawio-app` | the web client's ~150 MB of assets |
| `page` | `/ui/drawio` | the reception page |

The control WS does not cover mounts, so the static mount runs its own
register/hold-lease/reconnect loop — records are in-memory, and without it the
editor would 404 after any gateway restart.

## Install

    bash install.sh

Editable-installs `config`, `gatewayclient`, `persistence` and this service into
the `awm` env (override with `AWM_ENV=<name>`), and writes a gitignored
`.runtime-env` sidecar baking `AWM_PYTHON` so the gateway can respawn the
service under systemd's minimal PATH.

It then clones upstream `jgraph/drawio` at a pinned tag into `webapp/` and
applies three patches:

1. **`app.min.js`** — inject `window.__drawioUi=x;` after the `App`
   construction. Without it `PreConfig.js` never attaches and the editor looks
   fine while saving nothing, so the install **fails loudly** if the anchor is
   not found exactly once. Moving `DRAWIO_TAG` means re-deriving this anchor.
   Applied **only on a fresh clone** — it is a text injection into a minified
   bundle, so re-running it would double the injection.
2. **`js/PreConfig.js`** — replaced with awm's client (revision-checked saves,
   the flush/push handshake, no polling).
3. **`js/PostConfig.js`** — replaced with upstream's stub plus
   `ellipticArcEdgeStyle` (below).

Patches 2 and 3 are whole-file replacements, so they are idempotent and
re-applied on **every** `install.sh` run. That is deliberate: fixing client-side
code is a re-run, not a 150 MB `DRAWIO_FORCE=1` re-clone.

`webapp/` is gitignored: it is a reproducible build, not source. Skip it with
`DRAWIO_SKIP_APP=1` when you only want the verbs — an agent can build a diagram
headlessly; only the browser editor needs those bytes.

### The elliptic-arc edge style

Diagrams in this store use a custom edge router, `ellipticArcEdgeStyle`, which
draws an edge as a true circular arc through both cell centres. The bulge is
set per-edge by `arcSagitta` (px), `arcSagittaFraction` (× chord length), or
`arcRadius` (circle radius as a multiple of chord length — *larger* N is
*flatter*), in that priority order, with a `window.ELLIPTIC_ARC_CONFIG` global
for tuning the un-tagged default from the console. Usage is an Edit Style line:

    edgeStyle=ellipticArcEdgeStyle;arcRadius=12;curved=1

It has to ship with the client because **an unregistered edge style is not an
error in mxGraph** — the edge quietly falls back to the default router. So a
missing `PostConfig.js` looks like every arc turning straight (or bezier, where
the style also sets `curved=1`) with nothing logged, no failed save, and no
change to the stored document. `prokaryotic_metabolism.drawio` alone carries
~225 edges that depend on it. This is the one patch whose absence is a pure
rendering regression, which is exactly why it is easy to lose.

The page needs a built `dist/` to be discovered:

    cd awm && npm run build      # or: bash scripts/build.sh

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control loop) |
| `awm-persistence` | resolves the per-service data dir |
| `httpx`, `websockets` | already adapter deps; used by the mount lease + export |
| `pygraphviz` *(optional)* | only for the `layout` operation |

Git is required at runtime. Docker is required only for `export`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `AWM_DRAWIO_ROOT` | `<AWM_DIR>/services/drawio/diagrams` | the store |
| `AWM_DRAWIO_CHECKOUTS` | `<AWM_DIR>/services/drawio/checkouts` | working copies |
| `DRAWIO_APP_ROOT` | `<service>/webapp` | the web client tree |
| `DRAWIO_MOUNT_PREFIX` | `/drawio-app` | origin path for the client |
| `DRAWIO_TAG` | `v29.6.6` | upstream release to clone |
| `DRAWIO_SKIP_APP` | *(unset)* | install verbs only |
| `DRAWIO_FORCE` | *(unset)* | re-clone even if `webapp/` exists |
| `DRAWIO_EXPORT_URL` | `http://127.0.0.1:8000` | export server |
| `DRAWIO_EXPORT_CONTAINER` | `drawio-export` | container name |

## Scope & caveats

- **Both writers must go through the service.** Editing a live diagram's file on
  disk directly is exactly the race this exists to remove. Use a checkout.
- **Concurrent appends conflict.** If an agent and a person both add cells to
  the same page, the additions land adjacently and git reports a conflict even
  though nothing semantically clashes. Honest, and resolvable by hand — but it
  is the most likely conflict you will actually see.
- **Stripping viewport state** means the file no longer restores scroll
  position. The client preserves page/zoom/scroll across reloads itself, so
  this is invisible in practice.
- **Editor tab counts leak on an unclean close.** The count decrements on
  `editor_close`; a tab killed with the browser never sends one, so it lingers
  in `service_status` / the page's `editors` badge and costs each merge one
  `FLUSH_TIMEOUT_S` (4s) wait while the service waits for an ack that cannot
  come. Bounded, and cleared by restarting the service — there is no TTL or
  heartbeat reaping.
- **No auth**, like every awm service. Anything that can reach the gateway can
  edit any diagram.

## Verify

    awm services list                      # drawio → running
    awm drawio service_status              # store, counts, mount, export container
    awm drawio create --save sandbox/test
    awm drawio list

    # the contract, end to end
    H=$(awm drawio checkout --save sandbox/test | jq -r .handle)
    awm drawio edit --handle "$H" --ops '[{"op":"add_node","id":"t/1","label":"hello"}]'
    awm drawio path --handle "$H"          # look at the file
    awm drawio url --handle "$H"           # look at the render
    awm drawio merge --handle "$H"
    awm drawio history --save sandbox/test

    # the page and the editor, through the HTTPS front
    curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:12100/ui/drawio/
    curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:12100/drawio-app/index.html

    # export is self-contained
    awm drawio export --save sandbox/test --format pdf

Then open `/ui/drawio/` in a real browser — ideally from another device — click
into a diagram, and confirm edits persist across a reload. The concurrency
behaviour only shows up with a real tab open: take a checkout, edit a different
page in the browser, then `update` and `merge`, and check both changes survive.
