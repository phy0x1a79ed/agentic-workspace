"""Hub adapter for the drawio service.

Boots drawio as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID``; there is no token.

Two registrations, one process — the pattern ``fileviewer`` established:

* the ``ServiceAdapter`` control WS (``kind=service`` at ``/svc/drawio``)
  carries the verbs and buys supervision;
* a separate ``kind=static`` **mount** at ``/drawio-app`` serves the drawio web
  client's own bytes (~150 MB of JS/CSS/images that must not travel through an
  RPC verb). The control WS does not cover mounts, so that registration runs
  its own register/hold-lease/reconnect loop and survives a gateway restart.

The editor tab talks to the service over ordinary verbs plus one emit topic
(``drawio:<save>``) rather than a session bridge: the traffic is a save every
few seconds and an occasional push, not a byte stream, and emit already gives
the ordered flush → acknowledge → land → push handshake that ``merge`` needs.

**Surface split.** Read, checkout and autopublish verbs project onto MCP so
agents can drive the whole workflow. The editor's own plumbing (``editor_save``,
``editor_flush_ack``, ``editor_open``, ``editor_close``) is ``cli``/``http``
only — it is a browser protocol, not something an agent should ever call, and
putting it on the MCP surface would just be a way to corrupt a diagram.

**Background work.** Three tasks are started here and never awaited: the static
mount's lease loop, the autopublish debounce loop, and the boot-time autopublish
reconciliation. Awaiting any of them would stop the adapter serving its control
WS. They are also the only code in this service with no caller to return an
error to, which is why they log their failures rather than raising.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.drawio.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.drawio import chrome, export, mount, store as store_mod
from awm.drawio.autopublish import AutoPublisher
from awm.drawio.checkout import Checkouts
from awm.drawio.service import Service
from awm.drawio.store import Store
from awm.drawio import view as view_mod
from awm.drawio.view import ViewNotifier, ViewServer

log = logging.getLogger("awm.drawio.hub_adapter")

_CLI_HTTP = ["cli", "http"]

ADAPTER: ServiceAdapter | None = None
SERVICE: Service | None = None
PUBLISHER: AutoPublisher | None = None
VIEW: ViewServer | None = None


API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- diagrams -----------------------------------------------------
        {
            "name": "list",
            "tool": "drawio_list",
            "description": (
                "List every saved diagram in its folder structure, with page "
                "count, size, last revision/author, how many editor tabs are "
                "open on it, and any checkouts currently in flight."
            ),
            "params": [],
        },
        {
            "name": "info",
            "tool": "drawio_info",
            "description": (
                "One diagram's detail: per-page names and cell counts, current "
                "revision and author, open checkouts, and the editor URL."
            ),
            "params": [{"name": "save", "type": "string", "required": True,
                        "description": "Diagram path, e.g. 'scadc/biomass-map'."}],
        },
        {
            "name": "cells",
            "tool": "drawio_cells",
            "description": (
                "Inspect a diagram's cells (id, page, kind, label) without "
                "reading megabytes of XML. Filter by page, by label, or by a "
                "substring of the cell id — the way to find what an id refers "
                "to before editing it."
            ),
            "params": [
                {"name": "save", "type": "string", "required": True},
                {"name": "page", "type": "string", "description": "Limit to one page."},
                {"name": "label", "type": "string",
                 "description": "Match cells whose label contains this."},
                {"name": "id_contains", "type": "string",
                 "description": "Match cells whose id contains this."},
            ],
            "timeout": 60,
        },
        {
            "name": "read",
            "tool": "drawio_read",
            "description": (
                "The raw canonical XML of a diagram, optionally at an older "
                "revision. Large — prefer 'cells' unless you need the document."
            ),
            "params": [
                {"name": "save", "type": "string"},
                {"name": "rev", "type": "string",
                 "description": "Revision to read (default: current)."},
                {"name": "handle", "type": "string",
                 "description": "Read a checkout's working copy instead."},
            ],
            "timeout": 60,
        },
        {
            "name": "create",
            "tool": "drawio_create",
            "description": "Create a new empty diagram at the given path.",
            "params": [{"name": "save", "type": "string", "required": True}],
        },
        {
            "name": "import",
            "tool": "drawio_import",
            "description": (
                "Import an existing .drawio file from the filesystem into the "
                "store as the first revision of a new diagram."
            ),
            "params": [
                {"name": "save", "type": "string", "required": True},
                {"name": "source", "type": "string", "required": True,
                 "description": "Absolute path to the .drawio file to import."},
            ],
            "timeout": 120,
        },
        {
            "name": "history",
            "tool": "drawio_history",
            "description": "Revisions of a diagram, newest first, with author and label.",
            "params": [
                {"name": "save", "type": "string", "required": True},
                {"name": "limit", "type": "integer", "description": "Default 50."},
            ],
        },
        {
            "name": "diff",
            "tool": "drawio_diff",
            "description": "Textual diff of a diagram between two revisions.",
            "params": [
                {"name": "save", "type": "string", "required": True},
                {"name": "a", "type": "string", "required": True},
                {"name": "b", "type": "string",
                 "description": "Second revision (default: current)."},
            ],
            "timeout": 60,
        },
        {
            "name": "restore",
            "tool": "drawio_restore",
            "description": (
                "Restore a diagram to an earlier revision, landed as a NEW "
                "revision — history is never rewritten, so a restore can itself "
                "be undone."
            ),
            "params": [
                {"name": "save", "type": "string", "required": True},
                {"name": "rev", "type": "string", "required": True},
            ],
            "timeout": 60,
        },
        {
            "name": "check",
            "tool": "drawio_check",
            "description": (
                "Verify every /files/... image reference in a diagram actually "
                "resolves. Catches the silent failure: fileviewer's mask hides "
                "some paths and a hidden file 404s exactly like a missing one, "
                "so the cell renders blank with nothing logged. Run before export."
            ),
            "params": [{"name": "save", "type": "string", "required": True}],
            "timeout": 60,
        },
        {
            "name": "export",
            "tool": "drawio_export",
            "description": (
                "Render a diagram to PDF/PNG/JPG/SVG, once. (For a file that "
                "should STAY current, use autopublish instead.) Image "
                "references are inlined server-side, so the output is "
                "self-contained and needs no network to display. Refuses if any "
                "reference is broken — a figure with a silently blank cell "
                "looks finished, so nobody goes looking for what is missing. "
                "Pass allow_broken to override. SVG is drawio's own native "
                "export (real text, cropped viewBox), rendered in a headless "
                "browser; the other formats use the export container."
            ),
            "params": [
                {"name": "save", "type": "string"},
                {"name": "format", "type": "string",
                 "description": "pdf (default), png, jpg, or svg."},
                {"name": "out", "type": "string",
                 "description": "Output path (default: the service's exports dir)."},
                {"name": "page", "type": "integer",
                 "description": "Render only this page index."},
                {"name": "scale", "type": "number", "description": "Default 1.0."},
                {"name": "handle", "type": "string",
                 "description": "Render a checkout's working copy instead."},
                {"name": "allow_broken", "type": "boolean",
                 "description": "Export even with unresolvable image references."},
            ],
            "timeout": 300,
        },
        {
            "name": "url",
            "tool": "drawio_url",
            "description": (
                "The editor URL for a diagram, or for a checkout of one — open "
                "it to look at the render. Origin-relative, so it works from any "
                "device that can reach the gateway."
            ),
            "params": [
                {"name": "save", "type": "string"},
                {"name": "handle", "type": "string",
                 "description": "Checkout handle; opens the working copy instead."},
            ],
        },
        {
            "name": "view_url",
            "tool": "drawio_view_url",
            "description": (
                "The URL that serves one page (or the whole document) as a "
                "live SVG. Place it in ANOTHER diagram — Insert > Image > URL, "
                "or paste a cell carrying it — and that image re-renders "
                "whenever the source page changes, so one diagram can embed "
                "another's page without a copy going stale. Fails here if the "
                "page name is wrong, rather than 404ing after it is placed."
            ),
            "params": [
                {"name": "save", "type": "string", "required": True,
                 "description": "Diagram path, e.g. 'scadc/biomass-map'."},
                {"name": "page", "type": "string",
                 "description": (
                     "Page NAME to serve (default: the whole document). Named, "
                     "not indexed, so reordering tabs cannot repoint it."
                 )},
            ],
        },
        {
            "name": "remove",
            "tool": "drawio_remove",
            "description": "Delete a diagram (recoverable: its history is kept).",
            "params": [{"name": "save", "type": "string", "required": True}],
            "surfaces": _CLI_HTTP,
        },

        # ---- autopublish links --------------------------------------------
        {
            "name": "autopublish",
            "tool": "drawio_autopublish",
            "description": (
                "Keep a file on disk continuously rendered from a diagram. "
                "One way: the diagram is the source, the file is a copy, and "
                "nothing is ever read back. Every accepted change to the "
                "diagram — browser save, agent merge, restore — re-renders the "
                "target, so a poster or paper figure stops needing anyone to "
                "remember to re-export. Renders once immediately so you find "
                "out here if it cannot. Links survive a service restart. "
                "Create as many as you like; one per page for a multi-page "
                "diagram. If the target's parent folder is ever deleted, the "
                "link is deleted too — it is never recreated."
            ),
            "params": [
                {"name": "save", "type": "string", "required": True,
                 "description": "Diagram path, e.g. 'scadc/biomass-map'."},
                {"name": "target", "type": "string", "required": True,
                 "description": (
                     "Absolute path of the file to keep current. Must end in "
                     "the format's extension, and its parent folder must "
                     "already exist — autopublish never creates directories."
                 )},
                {"name": "page", "type": "string",
                 "description": (
                     "Page NAME to publish (default: the whole document). "
                     "Named, not indexed, so reordering tabs in the editor "
                     "cannot silently repoint the link at a different page."
                 )},
                {"name": "format", "type": "string",
                 "description": "svg (default), pdf, png, or jpg."},
                {"name": "scale", "type": "number", "description": "Default 1.0."},
            ],
            "timeout": 300,
        },
        {
            "name": "autopublish_list",
            "tool": "drawio_autopublish_list",
            "description": (
                "Every autopublish link, optionally just one diagram's. Shows "
                "what each link targets and the revision it last published, so "
                "a link whose revision trails the diagram's is one that has "
                "been failing — the service log says why."
            ),
            "params": [{"name": "save", "type": "string",
                        "description": "Limit to one diagram."}],
        },
        {
            "name": "autopublish_stop",
            "tool": "drawio_autopublish_stop",
            "description": (
                "Stop an autopublish link. The file it already published is "
                "left on disk — something may be consuming it."
            ),
            "params": [{"name": "id", "type": "string", "required": True,
                        "description": "Link id from autopublish_list."}],
        },
        {
            "name": "autopublish_now",
            "tool": "drawio_autopublish_now",
            "description": (
                "Force autopublish links to re-render right now and report "
                "what happened, by link id, by diagram, or all of them. This "
                "is how you find out why a published file looks stale: links "
                "keep the last good render on any failure and record nothing "
                "on themselves, so the reason lives here and in the log."
            ),
            "params": [
                {"name": "id", "type": "string",
                 "description": "One link. Omit to do a whole diagram or all."},
                {"name": "save", "type": "string",
                 "description": "Every link on this diagram."},
            ],
            "timeout": 300,
        },

        # ---- checkouts ----------------------------------------------------
        {
            "name": "checkout",
            "tool": "drawio_checkout",
            "description": (
                "Take a working copy of a diagram and get back a handle. Edit it "
                "for as long as you like while someone else edits the live "
                "diagram in the browser; nothing you do is visible to them, and "
                "nothing they do disturbs your layout, until you merge. Pass the "
                "handle to edit / path / url / status / update / merge / discard."
            ),
            "params": [{"name": "save", "type": "string", "required": True}],
        },
        {
            "name": "edit",
            "tool": "drawio_edit",
            "description": (
                "Apply editing operations to a checkout. All-or-nothing: if any "
                "operation fails, nothing is written. Operations are idempotent "
                "by cell id, so re-running a build updates instead of "
                "duplicating, and cells you did not name are never touched.\n"
                "Ops: add_node {page,id,label,style,at|right_of|below|in,gap,size}, "
                "add_edge {page,id,from,to,label,style}, "
                "set {id,page,label,style,move,size}, remove {id,page}, "
                "remove_prefix {prefix,page}, add_page {name,width,height}, "
                "layout {page,algo,rankdir}."
            ),
            "params": [
                {"name": "handle", "type": "string", "required": True},
                {"name": "ops", "type": "array", "required": True,
                 "description": "List of operation objects (see description)."},
            ],
            "timeout": 120,
        },
        {
            "name": "externalize",
            "tool": "drawio_externalize",
            "description": (
                "Rewrite a checkout's embedded image payloads "
                "(image=data:...) as /files/... filesystem references, so a "
                "re-rendered figure reaches the diagram on reload instead of "
                "needing a re-import. Matching is by exact content hash, never "
                "by filename — a name-based guess could silently swap one "
                "structure for another in a published figure. Anything "
                "unmatched is left embedded and reported."
            ),
            "params": [
                {"name": "handle", "type": "string", "required": True},
                {"name": "roots", "type": "array", "required": True,
                 "description": (
                     "Directories to search for the source images. ORDER IS "
                     "PRECEDENCE: when the same image content exists under "
                     "more than one root, the earliest root wins. List the "
                     "current render directory first — pointing a reference at "
                     "an archive copy resolves and renders correctly today, so "
                     "nothing fails, but a later re-render never reaches the "
                     "diagram. Prefer naming the exact directories over one "
                     "parent, since a parent sweeps up its own archive subdirs."
                 )},
            ],
            "timeout": 300,
        },
        {
            "name": "path",
            "tool": "drawio_path",
            "description": (
                "The filesystem path of a checkout's file — for looking at it, "
                "or for hand-editing when an edit is easier to express directly "
                "than as operations. Never edit a LIVE diagram this way; that is "
                "the race this service exists to remove."
            ),
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "status",
            "tool": "drawio_status",
            "description": (
                "Where a checkout stands: whether it has changes to land "
                "(ahead), how many revisions the live diagram has moved since "
                "(behind), and whether unresolved conflict markers remain."
            ),
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "update",
            "tool": "drawio_update",
            "description": (
                "Pull the live diagram's changes into your checkout. Clean "
                "merges just apply. Genuine conflicts are REPORTED, not guessed: "
                "the file gets ordinary <<<<<<< markers, you edit it by hand at "
                "`path`, then call `resolve`. This is the only place "
                "reconciliation happens — by the time you merge, landing is "
                "atomic and cannot produce a document neither side asked for."
            ),
            "params": [{"name": "handle", "type": "string", "required": True}],
            "timeout": 120,
        },
        {
            "name": "resolve",
            "tool": "drawio_resolve",
            "description": (
                "Declare a hand-resolved checkout clean. Refuses while conflict "
                "markers remain or the file no longer parses as a diagram."
            ),
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "merge",
            "tool": "drawio_merge",
            "description": (
                "Land your checkout onto the live diagram as one transaction. "
                "Live editor tabs are told to flush and hold first, so an "
                "in-flight autosave cannot revert the merge. Refuses if the "
                "checkout is behind or conflicted — call update first."
            ),
            "params": [
                {"name": "handle", "type": "string", "required": True},
                {"name": "label", "type": "string",
                 "description": "Revision label for the history entry."},
                {"name": "keep", "type": "boolean",
                 "description": "Keep the checkout open after landing (default false)."},
            ],
            "timeout": 180,
        },
        {
            "name": "discard",
            "tool": "drawio_discard",
            "description": "Throw away a checkout and everything in it.",
            "params": [{"name": "handle", "type": "string", "required": True}],
        },
        {
            "name": "checkouts",
            "tool": "drawio_checkouts",
            "description": "Every checkout currently open, optionally for one diagram.",
            "params": [{"name": "save", "type": "string"}],
        },

        # ---- editor plumbing (browser only) -------------------------------
        {
            "name": "editor_open",
            "description": "Editor tab announces it is showing a diagram.",
            "params": [{"name": "save", "type": "string", "required": True},
                       {"name": "tab", "type": "string", "required": True},
                       {"name": "handle", "type": "string"}],
            "surfaces": _CLI_HTTP,
        },
        {
            "name": "editor_save",
            "description": (
                "Editor tab autosave. Carries the revision the tab is based on; "
                "a stale save is rejected rather than applied."
            ),
            "params": [{"name": "save", "type": "string", "required": True},
                       {"name": "xml", "type": "string", "required": True},
                       {"name": "base_rev", "type": "string"},
                       {"name": "tab", "type": "string"},
                       {"name": "handle", "type": "string"}],
            "surfaces": _CLI_HTTP,
            "timeout": 120,
        },
        {
            "name": "editor_flush_ack",
            "description": "Editor tab confirms it has flushed for a pending merge.",
            "params": [{"name": "save", "type": "string", "required": True},
                       {"name": "tab", "type": "string", "required": True}],
            "surfaces": _CLI_HTTP,
        },
        {
            "name": "editor_close",
            "description": "Editor tab is going away.",
            "params": [{"name": "save", "type": "string", "required": True},
                       {"name": "tab", "type": "string", "required": True}],
            "surfaces": _CLI_HTTP,
        },
        {
            "name": "status_service",
            "tool": "drawio_service_status",
            "description": (
                "Service health: store location, diagram and checkout counts, "
                "open editor tabs, and whether the web-client mount is up."
            ),
            "params": [],
        },
    ],
    "emitters": [
        {
            "name": "diagram",
            "topic": "drawio:<save>",
            "description": (
                "Live coordination with editor tabs: {'type':'flush'} asks tabs "
                "to save now and hold before a merge lands; {'type':'push','rev'} "
                "tells them the diagram moved and to reload."
            ),
        },
    ],
    "sessions": [],
}


# -- handlers ---------------------------------------------------------------

def _svc() -> Service:
    if SERVICE is None:  # pragma: no cover — set in main() before serving
        raise RuntimeError("drawio service not initialized")
    return SERVICE


def _pub() -> AutoPublisher:
    if PUBLISHER is None:  # pragma: no cover — set in main() before serving
        raise RuntimeError("drawio service not initialized")
    return PUBLISHER


def _author(as_: str | None) -> str:
    """Who a revision is attributed to. The gateway passes caller identity as
    ``as_``; fall back to a name that is still greppable in history."""
    return as_ or "agent"


HANDLERS: dict[str, Any] = {
    "list": lambda a: _svc().list(),
    "info": lambda a: _svc().info(a["save"]),
    "cells": lambda a: _svc().cells(a["save"], page=a.get("page"),
                                    label=a.get("label"),
                                    id_contains=a.get("id_contains")),
    "read": lambda a: _svc().read(a.get("save"), rev=a.get("rev"),
                                  handle=a.get("handle")),
    "create": lambda a, as_=None: _svc().create(a["save"], author=_author(as_)),
    "import": lambda a, as_=None: _svc().import_file(a["save"], a["source"],
                                                     author=_author(as_)),
    "history": lambda a: _svc().history(a["save"], limit=int(a.get("limit") or 50)),
    "diff": lambda a: _svc().diff(a["save"], a["a"], a.get("b")),
    "restore": lambda a, as_=None: _svc().restore(a["save"], a["rev"],
                                                  author=_author(as_)),
    "check": lambda a: _svc().check(a["save"]),
    "export": lambda a: _svc().export(
        a.get("save"), fmt=a.get("format") or "pdf", out=a.get("out"),
        page=a.get("page"), scale=float(a.get("scale") or 1.0),
        allow_broken=bool(a.get("allow_broken")), handle=a.get("handle")),
    "url": lambda a: _svc().url(a.get("save"), handle=a.get("handle")),
    "view_url": lambda a: _svc().view_url(a["save"], page=a.get("page")),
    "remove": lambda a, as_=None: _svc().remove(a["save"], author=_author(as_)),

    "autopublish": lambda a, as_=None: _pub().create(
        a["save"], a["target"], page=a.get("page"),
        fmt=a.get("format") or "svg", scale=float(a.get("scale") or 1.0),
        author=_author(as_)),
    "autopublish_list": lambda a: _pub().list(a.get("save")),
    "autopublish_stop": lambda a: _pub().stop(a["id"]),
    "autopublish_now": lambda a: _pub().now(a.get("id"), a.get("save")),

    "checkout": lambda a, as_=None: _svc().checkout(a["save"], author=_author(as_)),
    "edit": lambda a: _svc().edit(a["handle"], a["ops"]),
    "externalize": lambda a: _svc().externalize(a["handle"], a["roots"]),
    "path": lambda a: _svc().path(a["handle"]),
    "status": lambda a: _svc().status(a["handle"]),
    "update": lambda a: _svc().update(a["handle"]),
    "resolve": lambda a: _svc().resolve(a["handle"]),
    "merge": lambda a: _svc().merge(a["handle"], label=a.get("label"),
                                    keep=bool(a.get("keep"))),
    "discard": lambda a: _svc().discard(a["handle"]),
    "checkouts": lambda a: {
        "checkouts": [h.as_dict() for h in _svc().checkouts.list(a.get("save"))],
    },

    "editor_open": lambda a: _editor_open(a),
    "editor_save": lambda a: _svc().save_from_editor(
        a["save"], a["xml"], a.get("base_rev"), tab_id=a.get("tab") or "tab",
        handle=a.get("handle")),
    "editor_flush_ack": lambda a: _editor_ack(a),
    "editor_close": lambda a: _editor_close(a),
    "status_service": lambda a: _status_service(),
}


def _editor_open(args: dict) -> dict:
    service = _svc()
    service.note_tab(store_mod.normalize_save_path(args["save"]), args["tab"])
    return service.info(args["save"])


def _editor_ack(args: dict) -> dict:
    _svc().note_flush_ack(store_mod.normalize_save_path(args["save"]), args["tab"])
    return {"ok": True}


def _editor_close(args: dict) -> dict:
    _svc().drop_tab(store_mod.normalize_save_path(args["save"]), args["tab"])
    return {"ok": True}


def _status_service() -> dict:
    service = _svc()
    return {
        "store": str(service.store.root),
        "checkouts_root": str(service.checkouts.root),
        "diagrams": len(service.store.list()),
        "open_checkouts": len(service.checkouts.list()),
        "editor_tabs": {save: len(tabs)
                        for save, tabs in service.live_tabs.items() if tabs},
        "app_mount": mount.status(),
        "view_mount": VIEW.status() if VIEW is not None else {"mounted": False},
        # Two render backends: the container does pdf/png/jpg, headless Chrome
        # does svg (drawio has no server-side SVG renderer). Both are reported
        # because either being down silently stops a different set of exports.
        "export_container": export.container_state(),
        "svg_browser": chrome.state(),
        "autopublish": _pub().status(),
    }


# -- startup ----------------------------------------------------------------

async def _emit(topic: str, payload: Any) -> None:
    if ADAPTER is not None:
        await ADAPTER.emit(topic, payload)


def _on_start() -> None:
    """Stand the store up and launch the background loops.

    ``hold_mount`` and the autopublish loop never return, so they must be tasks
    rather than awaited — awaiting either would stop the adapter from ever
    serving its control WS. Reconciliation is a task for the same reason: a
    slow catch-up render must not delay the service coming up.
    """
    global SERVICE, PUBLISHER, VIEW
    store = Store(store_mod.default_root())
    from awm.drawio.checkout import default_root as checkouts_root

    SERVICE = Service(store, Checkouts(store, checkouts_root()), emit=_emit)
    PUBLISHER = AutoPublisher(store)
    PUBLISHER.attach(store)

    # The view layer: a URL that returns a page's live SVG. Two commit
    # subscribers — prune the cache a rename/removal orphaned, and tell
    # subscribed consumers the page moved so they re-fetch their placed image.
    VIEW = ViewServer(store)
    store.subscribe(VIEW.renderer.prune_for_commit)
    ViewNotifier(_emit).attach(store)

    asyncio.create_task(mount.hold_mount())
    asyncio.create_task(VIEW.hold_mount())
    asyncio.create_task(PUBLISHER.run())
    asyncio.create_task(PUBLISHER.reconcile())
    log.info("drawio store at %s (%d diagram(s)), %d autopublish link(s), "
             "app mount %s → %s, view mount %s", store.root, len(store.list()),
             len(PUBLISHER.registry.links), mount.MOUNT_PREFIX, mount.app_root(),
             view_mod.VIEW_PREFIX)


async def main() -> None:
    global ADAPTER
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ADAPTER = ServiceAdapter("drawio", API_MANIFEST, HANDLERS, on_start=_on_start)
    await ADAPTER.run()


if __name__ == "__main__":
    asyncio.run(main())
