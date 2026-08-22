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
updates the diagram on reload instead of requiring a re-import. A cell can also
reference *another diagram's page* through the view mount (below); both kinds
are origin-relative, and both are resolved to embedded data at export time.

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

Inlining covers both reference kinds: a `/files` reference becomes the file's
bytes, and a placed page view becomes *that page rendered*, honouring the
reference's own query, so two placements of one page in different colours embed
as two different images. Both are matched by a single alternation, which is what
keeps a diagram stored under a path containing `files` from having the `/files/…`
*inside its own view URL* rewritten. In stored XML a multi-parameter query is
spelled `&amp;`, so the matcher admits an XML ampersand entity — a terminator set
that stopped at `;` would cut the URL at `&amp` and drop every parameter but the
first, silently. Because a placed page can itself place another, resolution is
bounded three ways: nesting depth, total nested renders, and total inlined bytes
(nesting re-encodes, so it roughly doubles per level). A cycle is keyed on
`(save, page)` — a page embedding itself at a different scale is still a cycle —
and is reported, never hung.

Inlining strictly **precedes** the render call at every call site. The headless
browser's lock is not re-entrant, and a nested page render happens during
inlining; moving inlining inside the render would self-deadlock.

**SVG does not go through the container.** It cannot: that server answers
`400 Unsupported Format!` for `svg`, because drawio has always produced SVG
client-side — `mxGraph.getSvg()` walks the live graph — and there is no
server-side equivalent to call. So SVG is rendered by loading drawio's own
`export3.html` in **headless Chrome** and asking the drawn graph to serialize
itself (`awm/drawio/chrome.py`). The output is what the editor's *Export as SVG*
gives you: real `<text>` elements, a viewBox cropped to the drawing,
`data-cell-id` on every shape — roughly a tenth the size of a traced PDF, and
still selectable and styleable.

The browser is driven over the Chrome DevTools Protocol — JSON over a WebSocket,
using the `httpx` + `websockets` this service already depends on, so there is no
puppeteer/playwright install and no bundled browser download. One headless
Chrome is kept alive between renders (a relaunch each time would cost ~1s, and
autopublish can render every few seconds); it is left in the service's process
group so the hub supervisor's group kill takes it down rather than orphaning it.

The page is loaded from the gateway's own `/drawio-app` mount rather than
`file://`, because the client pulls stencils and fonts over XHR, which `file://`
blocks. So **SVG export needs the gateway mount up**; PDF and PNG do not.

### Autopublish — keeping a file rendered

`drawio autopublish` is the standing version of `export`: *this diagram, this
page, rendered to this path, kept current*. Every accepted write to the diagram
— browser save, agent `merge`, `restore` — re-renders every link on it, so a
poster or paper figure stops needing anyone to remember to re-export.

    drawio autopublish --save <save> --target /abs/path.svg [--page <name>]
    drawio autopublish_list [--save <save>]
    drawio autopublish_stop --id <id>
    drawio autopublish_now [--id <id> | --save <save>]

Four properties, each avoiding a specific failure:

- **One way, always.** Nothing reads the target back; a link cannot carry an
  edit backwards into the store.
- **Replace, never write in place.** The render goes to a sibling `.tmp` and is
  `os.replace`d on. A LaTeX build reading at the wrong moment sees the old file
  or the new one, never half of either.
- **Last good stays.** A failed render — broken `/files` refs, container or
  browser down, the named page gone — leaves the published file untouched and
  writes the reason to the service log. Nothing is recorded on the link itself:
  links are configuration, not health. `autopublish_now` re-renders on demand
  and reports what happened.
- **The destination owns the link's life.** If the target's parent directory has
  gone away, the link is deleted rather than recreating it — a directory that
  vanished was deleted on purpose.

Pages are addressed by **name**, not index: reordering tabs in the editor shifts
every index, and a link that quietly starts publishing a different page is worse
than one that stops. A multi-page diagram published in full is one link per page.

The reception page's **Publish** tab is the same four verbs as a form plus a
managed list, with an *all diagrams* toggle. Two things it derives rather than
reads, because a link stores no health: a row is flagged when its `last_rev`
trails its diagram's, and when its page name is no longer in that diagram. It
also reports a `published: false` first render as a failure — the verb *returns*
that outcome rather than raising, so a link created but never rendered would
otherwise read as success.

Links live in `<AWM_DIR>/services/drawio/autopublish.json` and survive a restart;
on boot every link is reconciled, so an edit made while the service was down is
caught up. Writes are debounced — a diagram must go quiet for `DEBOUNCE_SECONDS`
(3s) before rendering, so an editing burst costs one render rather than one per
autosave, with a `MAX_DEFER_SECONDS` (30s) ceiling so a continuously-edited
diagram still publishes.

### View URL — a page's live SVG, placeable into another diagram

`autopublish` renders *to a file*; the view URL renders *to a URL*, so a page can
be dropped into another diagram straight from drawio's own **Insert → Image →
URL** and behave like any placed image — movable, selectable, and kept current
while the consumer is open.

    GET  /drawio-app/view/<save>/<page>        → image/svg+xml (that page)
    GET  /drawio-app/view/<save>               → the whole/first page
    HEAD /drawio-app/view/<save>/<page>        → resolves save/page/rev, no render

    ?swap=<from>:<to>   repeatable — replace one colour with another
    ?crop=<name>        render only the shape with that label (or cell id)
    ?scale=<n>

`drawio view_url --save <save> [--page <name>]` is the one authority for
building that URL — it percent-encodes each segment (a `/` or `;` in a page name
would otherwise split the path or truncate the drawio style string the URL ends
up inside) and validates the page name, so a wrong name fails there rather than
404ing after the image has been placed. The reception page's **Pages** tab uses
it for two per-page buttons: *copy url* for the Insert dialog, and *copy cell*,
which puts a one-cell `<mxGraphModel>` fragment sized to the render's own aspect
on the clipboard — pasting that into any open diagram places the image directly,
no dialog. When the browser has no clipboard (it needs a secure origin) the text
is offered for manual copying rather than silently doing nothing.

The last path segment is the page **name** (a trailing `.svg` is accepted and
ignored); everything before it is the diagram path. Otherwise the render is
drawio's native *Export as SVG* — transparent background, 0 border, a viewBox
cropped to the drawing. A missing diagram, unknown page or unknown crop name is
an honest `404`, never a blank image; a malformed parameter is a `400` naming
the token, including the empty `?swap=` case (the handler keeps blank values
precisely so that one cannot vanish into a plain render).

**The URL is the variable.** `swap` and `crop` are what let *one* source page
serve every placement, instead of keeping `plasmid-red`, `plasmid-green` and
`plasmid-blue` and repeating every edit three times. Draw the region that should
vary in a mask colour — `#ff00ff` is the convention, since nobody picks it on
purpose — and recolour per placement. Both parameters are parsed, formatted and
fingerprinted by `awm/drawio/renderspec.py`, which is the *only* place that
grammar lives: the HTTP handler, `view_url`, an autopublish link and the
export-time inliner all go through it, so a parameter cannot exist on one
surface and silently not on another. Order never matters — the formatter and the
fingerprint both sort.

A swap reaches the picture whatever the picture is made of: style values whose
key ends in `Color` (plus `imageBorder`/`imageBackground`), colours inside a
cell's label, the text of referenced `/files/**.svg` images, and the contents of
an `image=` value — which drawio writes as base64 whenever you import an SVG, and
which is therefore *decoded* rather than text-replaced. `awm/drawio/recolour.py`
owns that: it sniffs the codec off the payload, recolours SVG source as text and
PNG/JPEG/GIF/WebP as pixels, and descends into a raster nested inside an imported
SVG. A raster mask is shifted by `target - source` within a tolerance rather than
filled flat, so shading and JPEG noise survive; an image containing none of the
source colours comes back byte-identical and never re-encoded, because the cache
key is a hash of the inlined document. A swap that cannot be *attempted* — Pillow
absent, an image that will not decode — is reported through `X-Drawio-Problems`
rather than passing for a picture that simply had no mask in it.

One `image=` value is deliberately left alone: another page's view URL. A
reference's colours are decided by its own query, so the enclosing page's swaps
never cascade into an embedded page.

Replacements are simultaneous, never chained (one compiled alternation, one
pass), and named colours / `rgb()` / eight-digit `#rrggbbaa` are out of scope,
the last rejected rather than half-supported. A compressed diagram fails loudly
instead of returning an un-swapped render that looks like a swap which matched
nothing.

`crop` names a shape by label (or cell id). The frame is restyled to a bare
outline before rendering — drawio builds no state for an invisible cell, so a
hidden frame is one the browser cannot measure — then measured off the rendered
SVG via `data-cell-id` and deleted from the output, so it never appears and the
author does not have to make it invisible. Moving or resizing the frame changes
the page's content, which busts every placement on the next change event.

Cropping narrows the `viewBox`; it does not delete geometry. Shapes outside the
frame are clipped everywhere the SVG is displayed, but they are still *in* the
file — worth knowing before cropping is used to keep something out of a figure
somebody else will read.

It is a fourth registration — a loopback listener fronted as a `kind=url` mount at
`/drawio-app/view`. A service's browser surface is `POST /svc/…/fn/…` (no
GET-to-verb) and a `kind=static` mount cannot render on demand, so the listener
is what turns a GET into a render. The gateway's longest-prefix routing resolves
`/drawio-app/view/…` to it and everything else under `/drawio-app` to the editor.

**One page's work, and no more.** The document is cut to the requested page
before anything else happens, so a request resolves only the references that page
places. Without that, asking for any page of a diagram whose *other* pages hold
live views resolved all of them, recursively, and then threw the result away.
Problems belonging to other pages therefore no longer reach `X-Drawio-Problems`;
`drawio check` is what audits a whole document.

**Cache.** Each render is cached under
`<AWM_DIR>/services/drawio/viewcache/<save>/<page>/<hash>.svg`, keyed by the
SHA-256 of the page's *inlined* XML — so a diagram edit **or** a changed
referenced `/files` image busts it, while an unchanged page keeps its cache
across an unrelated commit. Responses carry that hash as an `ETag` with
`Cache-Control: no-cache`, so a re-fetch after a change collapses to a `304` when
nothing actually moved. A gone page (rename/removal) or a removed diagram has its
cache directory pruned on the next commit — the destination owns the cache the
same way an autopublish link owns its file. Only the newest
`MAX_VERSIONS_PER_PAGE` (5) renders per page are kept.

Each **variant** (a non-empty query) caches in its own subdirectory under the
page, so the version cap applies per variant rather than letting three colours
of one plasmid evict each other on every edit; the query space is
caller-controlled, so `MAX_VARIANTS_PER_PAGE` (12) reclaims abandoned ones. The
un-parameterised render keeps the original path and key, which is why deploying
variants threw no already-rendered page away. Variants of a page share one
change topic and therefore refresh together — they have one source.

In front of that key sits a precheck that answers from `stat` alone: the
diagram's `(mtime_ns, size)`, every referenced file's, and the spec. On a hit the
request returns the stored render without parsing, inlining or forking `git` for
the revision header. It is strictly a fast path — the `ETag` is still the content
key, a miss costs the full pass, and a request pinned to a revision never takes
it, because the working tree's stat says nothing about history.

The browser leg keeps one tab parked on drawio's `export3.html` rather than
loading the ~9 MB app bundle per render. Any anomaly — the tab gone, the reset
failing, the render not answering — discards it and falls back to
create-navigate-render, so reuse can never wedge the service.

**Live update in an open consumer.** On every accepted write the service emits
`{"type":"view-updated","save","rev"}` on the emit topic for the page that
actually changed — `drawio:<save>:<page>`, percent-encoded, so an edit to one page
does not wake every consumer of its siblings. The client (`PreConfig.js`)
recognizes view-URL images on the *active* page, learns each one's source diagram
and page via a `HEAD` (`X-Drawio-Save` / `X-Drawio-Page`), subscribes to that
topic, and on an event re-fetches just those images — a swap on the rendered
`<image>` node only, never a model edit, so a consumer never autosaves a churned
URL and its document stays byte-for-byte canonical. Insert the image as a **link**
(URL kept) for this; an embedded copy is a one-time snapshot by definition.

**Last-seen images.** `Cache-Control: no-cache` means the browser must revalidate
before it may paint, and revalidating costs a server render pass — so reopening a
consumer showed empty boxes until every image round-tripped. The client keeps the
bytes it last saw in IndexedDB (keyed by the URL without its `?rev=` buster,
bounded by count and total size) and paints them at first draw with no network at
all. The revalidation behind that sends `If-None-Match` and touches the DOM only
on a new `ETag`. Nothing about it writes to the model.

### Registrations

Four, named `drawio` except the view mount (the registry keys records on
`(kind, name)`, so the view mount takes a distinct name):

| kind | name | prefix | what |
|---|---|---|---|
| `service` | `drawio` | `/svc/drawio` | the verbs, plus supervision |
| `static` | `drawio` | `/drawio-app` | the web client's ~150 MB of assets |
| `page` | `drawio` | `/ui/drawio` | the reception page |
| `url` | `drawio-view` | `/drawio-app/view` | the loopback listener that renders a page's live SVG |

The control WS does not cover mounts, so the static mount and the view mount each
run their own register/hold-lease/reconnect loop — records are in-memory, and
without it they would 404 after any gateway restart.

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
| `httpx`, `websockets` | already adapter deps; used by the mount lease, export, and the CDP browser driver |
| `pillow`, `numpy` | recolouring a raster image: decode with one, do the colour pass in the other |
| `pygraphviz` *(optional)* | only for the `layout` operation |

Git is required at runtime. Docker is required for `export` to PDF/PNG/JPG. A
Chrome or Chromium binary is required for `export`/`autopublish` to **SVG** —
see *Export* above for why a browser is unavoidable there. Neither is needed for
any other verb.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `AWM_DRAWIO_ROOT` | `<AWM_DIR>/services/drawio/diagrams` | the store |
| `AWM_DRAWIO_CHECKOUTS` | `<AWM_DIR>/services/drawio/checkouts` | working copies |
| `AWM_DRAWIO_AUTOPUBLISH` | `<AWM_DIR>/services/drawio/autopublish.json` | the autopublish link registry |
| `AWM_DRAWIO_VIEWCACHE` | `<AWM_DIR>/services/drawio/viewcache` | rendered-page cache for the view URL |
| `DRAWIO_APP_ROOT` | `<service>/webapp` | the web client tree |
| `DRAWIO_MOUNT_PREFIX` | `/drawio-app` | origin path for the client |
| `DRAWIO_VIEW_PREFIX` | `/drawio-app/view` | origin path for the live-SVG view mount |
| `DRAWIO_TAG` | `v29.6.6` | upstream release to clone |
| `DRAWIO_SKIP_APP` | *(unset)* | install verbs only |
| `DRAWIO_FORCE` | *(unset)* | re-clone even if `webapp/` exists |
| `DRAWIO_EXPORT_URL` | `http://127.0.0.1:8000` | export server (pdf/png/jpg) |
| `DRAWIO_EXPORT_CONTAINER` | `drawio-export` | container name |
| `DRAWIO_CHROME` | *(first found on PATH)* | browser binary for SVG export |
| `DRAWIO_EXPORT_PAGE` | `$AWM_HUB_URL/drawio-app/export3.html` | drawio's export client, for SVG |

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
    awm drawio service_status              # store, counts, mount, both render backends
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

    # export is self-contained — pdf via the container, svg via headless Chrome
    awm drawio export --save sandbox/test --format pdf
    awm drawio export --save sandbox/test --format svg

    # autopublish: the file stays current on its own
    mkdir -p /tmp/pub
    awm drawio autopublish --save sandbox/test --target /tmp/pub/test.svg
    awm drawio autopublish_list
    # edit the diagram (browser or checkout+merge), wait ~3s, then:
    ls -l /tmp/pub/test.svg                # mtime and content moved by itself
    rm -rf /tmp/pub                        # …and the link drops itself
    awm drawio autopublish_now             # forces a pass; reports what happened

    # the view URL — a page's live SVG, served and cached on demand
    awm drawio view-url --save sandbox/test --page Page-1   # 404s here on a bad name
    curl -sk -H 'Accept: image/svg+xml' \
      https://127.0.0.1:12100/drawio-app/view/sandbox/test/Page-1 | head -c 80
    curl -skI https://127.0.0.1:12100/drawio-app/view/sandbox/test/Page-1 \
      | grep -i 'content-type\|etag\|x-drawio'      # image/svg+xml + resolve headers

Then open `/ui/drawio/` in a real browser — ideally from another device — click
into a diagram, and confirm edits persist across a reload. The concurrency
behaviour only shows up with a real tab open: take a checkout, edit a different
page in the browser, then `update` and `merge`, and check both changes survive.

For the view URL end to end, the short path is the page's **Pages** tab: *copy
cell* on a page, then Ctrl-V in another diagram's tab — it places as a movable
image. Then edit that source page in another tab; the placed image refreshes on
its own within a few seconds, and the consumer diagram's saved XML is unchanged
(no autosave churn). The long path is the same thing by hand: **Insert → Image →
URL** with a `…/drawio-app/view/<save>/<page>` link, choosing **Link** (not a
copy) — an embedded copy is a one-time snapshot by definition.

For the **Publish** tab: start a link to a scratch directory, confirm the file
appears; edit the diagram and watch it move on its own; rename the published page
and confirm the row warns while the last render stays on disk; *stop* it (the
file is deliberately left behind); then delete the target's parent directory and
confirm the row disappears on the next poll.
