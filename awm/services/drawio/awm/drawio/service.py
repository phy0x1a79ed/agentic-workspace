"""Service-level orchestration: the store, the checkouts, and the live tabs.

Everything below the verbs lives here. Two responsibilities the lower layers
deliberately do not have:

**Landing against a live tab.** :meth:`Service.merge` cannot simply write: a
browser tab autosaving every couple of seconds moves the diagram's tip
continuously, so a naive "refuse while behind" loop would let an actively
editing person starve an agent forever. The fix is the order the design called
for from the start — tell live tabs to flush and hold, confirm the tip has not
moved, land, release, push the result back. The hold is sub-second and the
tabs cooperate, so the agent always gets a turn.

**Image references.** Cells point at ordinary files through fileviewer's
``/files/<abs-path>`` mount, or at another diagram's page through the view
mount, instead of carrying embedded data. That keeps a re-rendered figure live
in the diagram without a re-import, but it means a reference can break silently:
fileviewer's mask is a denylist and a masked path 404s exactly like a missing
one, so a cell whose image sits somewhere the mask covers renders blank with no
error anywhere — and a page view whose source page was renamed does the same.
:meth:`Service.check` is what turns both into a report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from . import mount, ops, renderspec, store as store_mod, xmlmodel
from .checkout import Behind, Checkouts, Conflicted
from .store import Store

log = logging.getLogger("awm.drawio.service")

#: How long :meth:`Service.merge` waits for live tabs to acknowledge a flush.
#: Long enough for a tab to serialize a multi-megabyte document and POST it,
#: short enough that a dead tab cannot stall an agent.
FLUSH_TIMEOUT_S = 4.0

#: How long the write lease keeps tabs from autosaving while a merge lands.
#: Tabs stay editable throughout; only the *save* is deferred.
LEASE_SECONDS = 10.0

class Lease:
    """A short exclusive window in which only the merging caller may write.

    Held for at most :data:`LEASE_SECONDS` so that a crashed merge cannot wedge
    a diagram permanently — the expiry is the safety net, not the mechanism.
    """

    def __init__(self) -> None:
        self.holder: str | None = None
        self.expires: float = 0.0

    def held(self) -> bool:
        return self.holder is not None and time.time() < self.expires

    def acquire(self, holder: str) -> bool:
        if self.held():
            return False
        self.holder, self.expires = holder, time.time() + LEASE_SECONDS
        return True

    def release(self, holder: str) -> None:
        if self.holder == holder:
            self.holder, self.expires = None, 0.0


class Service:
    """The verb surface. Sync throughout except :meth:`merge`, which coordinates
    with live browser tabs and therefore has to await them."""

    def __init__(self, store: Store, checkouts: Checkouts,
                 emit: Callable[[str, Any], Awaitable[None]] | None = None,
                 user: str | None = None):
        self.store = store
        self.checkouts = checkouts
        self._emit = emit
        # The owning user of a per-user store; None for the legacy store.
        # Prefixes the emit topic (the public edge admits a subscriber only to
        # its own user's topics) and the editor's author handle.
        self.user = user
        self._leases: dict[str, Lease] = {}
        self._merge_locks: dict[str, asyncio.Lock] = {}
        # save -> {tab_id: last_seen}. Populated by the editor session layer.
        self.live_tabs: dict[str, dict[str, float]] = {}
        self._flush_acks: dict[str, set[str]] = {}

    def topic_of(self, save: str) -> str:
        """The emit topic editor tabs on ``save`` subscribe to."""
        return f"drawio:{self.user}:{save}" if self.user else f"drawio:{save}"

    # --- diagram surface ---------------------------------------------------

    def list(self) -> dict:
        diagrams = self.store.list()
        open_checkouts: dict[str, list] = {}
        for handle in self.checkouts.list():
            open_checkouts.setdefault(handle.save, []).append(handle.as_dict())
        for diagram in diagrams:
            diagram["checkouts"] = open_checkouts.get(diagram["save"], [])
            diagram["editors"] = len(self.live_tabs.get(diagram["save"], {}))
        return {"diagrams": diagrams, "count": len(diagrams)}

    def info(self, save: str) -> dict:
        info = self.store.info(save)
        info["checkouts"] = [h.as_dict() for h in self.checkouts.list(save)]
        info["editors"] = len(self.live_tabs.get(info["save"], {}))
        info["url"] = self.url(save)["url"]
        return info

    def create(self, save: str, author: str) -> dict:
        result = self.store.create(save, author=author)
        result["url"] = self.url(result["save"])["url"]
        return result

    def import_file(self, save: str, source: str, author: str) -> dict:
        result = self.store.import_file(save, source, author=author)
        result["url"] = self.url(result["save"])["url"]
        return result

    def read(self, save: str | None = None, rev: str | None = None,
             handle: str | None = None) -> dict:
        """The canonical XML of a live diagram, an old revision, or a checkout.

        One verb covers all three so the editor loads a checkout exactly the
        way it loads a live diagram — the browser never needs to know which it
        is looking at, and never needs a second transport to fetch bytes.
        """
        if handle:
            state = self.checkouts.status(handle)
            return {"save": state["save"], "handle": handle,
                    "rev": state["content_rev"],
                    "xml": self.checkouts.file_path(handle).read_text(
                        encoding="utf-8")}
        if not save:
            raise ValueError("read needs either a diagram path or a checkout handle")
        path = store_mod.normalize_save_path(save)
        # Always report which revision these bytes are, so a reader that saves
        # them back can be checked against it.
        return {"save": path, "rev": rev or self.store.head_rev(path),
                "xml": self.store.read(save, rev=rev)}

    def cells(self, save: str, page: str | None = None,
              label: str | None = None, id_contains: str | None = None) -> dict:
        """Inspect a diagram's cells without reading megabytes of XML."""
        from . import dwg

        doc = dwg.Doc(Path(self.store.abs_path(save)),
                      xmlmodel.parse(self.store.read(save)))
        if label or id_contains:
            hits = doc.find(label=label, id_contains=id_contains)
        else:
            hits = []
            for candidate in doc.pages():
                if page and candidate.name != page:
                    continue
                for cell in candidate.cells():
                    hits.append({
                        "id": cell.get("id"), "page": candidate.name,
                        "kind": dwg.cell_kind(cell), "value": cell.get("value", ""),
                    })
        return {"save": store_mod.normalize_save_path(save), "cells": hits,
                "count": len(hits)}

    def history(self, save: str, limit: int = 50) -> dict:
        return {"save": store_mod.normalize_save_path(save),
                "revisions": [r.as_dict() for r in self.store.history(save, limit)]}

    def diff(self, save: str, a: str, b: str | None = None) -> dict:
        return {"save": store_mod.normalize_save_path(save), "a": a, "b": b,
                "diff": self.store.diff(save, a, b)}

    def restore(self, save: str, rev: str, author: str) -> dict:
        return self.store.restore(save, rev, author=author)

    def remove(self, save: str, author: str) -> dict:
        return self.store.remove(save, author=author)

    def export(self, save: str, fmt: str = "pdf", out: str | None = None,
               page: int | None = None, scale: float = 1.0,
               allow_broken: bool = False, handle: str | None = None,
               swaps: list[str] | None = None, crop: str | None = None) -> dict:
        """Render a diagram to a file on disk.

        Refuses by default when any image reference is broken. A figure with a
        silently blank cell is worse than no figure at all: it looks finished,
        so nobody goes looking for what is missing. Override deliberately with
        ``allow_broken``.

        Takes the same ``swaps`` and ``crop`` a view URL takes, through the same
        parser, so "export what that link shows" needs no translation step.
        """
        from . import export as export_mod

        xml = (self.read(handle=handle)["xml"] if handle
               else self.store.read(save))
        parsed_swaps = renderspec.parse_swaps(swaps or ())
        source = self.checkouts.status(handle)["save"] if handle else \
            store_mod.normalize_save_path(save)

        if not allow_broken and not handle:
            report = self.check(source)
            if not report["ok"]:
                raise export_mod.ExportError(
                    f"{len(report['problems'])} image reference(s) will not "
                    "render; run `drawio check` for detail, or pass "
                    "allow_broken=true to export anyway"
                )

        data, problems = export_mod.render(xml, fmt, page=page, scale=scale,
                                           swaps=parsed_swaps, crop=crop)
        if problems and not allow_broken:
            raise export_mod.ExportError(
                "could not inline: " + "; ".join(problems))

        target = Path(out).expanduser() if out else Path(
            self.store.root).parent / "exports" / (
                source.replace("/", "_").removesuffix(".drawio") + f".{fmt}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {
            "save": source, "format": fmt, "path": str(target),
            "bytes": len(data), "problems": problems,
            # Origin-relative, so the caller can just open it.
            "url": f"/files{target}",
        }

    def url(self, save: str, handle: str | None = None) -> dict:
        """The editor URL for a diagram, or for a checkout of one.

        Origin-relative on purpose: the gateway is fronted by ``httpsfront``, so
        a relative link opens from a phone on the LAN exactly as it does from
        the host, and never bakes in a loopback address the way the prototype
        did.
        """
        if handle:
            self.checkouts.status(handle)  # raises if unknown
            return {"url": f"{mount.MOUNT_PREFIX}/index.html?checkout={handle}",
                    "handle": handle}
        path = store_mod.normalize_save_path(save)
        return {"url": f"{mount.MOUNT_PREFIX}/index.html?save={quote(path)}",
                "save": path}

    def view_url(self, save: str, page: str | None = None,
                 swaps: list[str] | None = None, crop: str | None = None,
                 scale: float | None = None) -> dict:
        """The URL that serves a page as a live SVG, for placing in a diagram.

        One authority for the prefix, the encoding, the page check and the
        parameters, so a caller never has to hand-assemble a path the renderer
        then 404s on. Origin-relative for the same reason :meth:`url` is.

        The page name is encoded with an empty safe set — a page called ``a/b``
        would otherwise split into two path segments and resolve to nothing,
        and a ``;`` reaching a drawio style string unescaped truncates it, which
        renders the placed cell blank rather than failing.

        ``swaps`` and ``crop`` are validated here rather than at first fetch: a
        typo'd colour should fail where it is written, not silently render the
        wrong picture from inside somebody else's diagram.
        """
        from . import view as view_mod
        from .autopublish import page_index

        path = store_mod.normalize_save_path(save)
        xml = self.store.read(path)  # raises UnknownDiagram
        index = page_index(xml, page)  # raises on a name this diagram does not have

        spec = renderspec.RenderSpec(
            scale=float(scale) if scale else 1.0,
            swaps=renderspec.parse_swaps(swaps or ()),
            crop=(crop or None),
        )
        if spec.crop:
            renderspec.prepare_crop(xml, index, spec.crop)  # raises CropNotFound

        url = f"{view_mod.VIEW_PREFIX}/{quote(path, safe='/')}"
        if page is not None:
            url += f"/{quote(page, safe='')}"
        query = renderspec.to_query(spec)
        if query:
            url += f"?{query}"
        # No save-vs-page ambiguity to report: the renderer prefers
        # save=all-but-last, and its fallback would need `<save>.drawio` to be a
        # directory as well as a file. Store paths always end in `.drawio`, so
        # the two readings can never both resolve.
        return {"url": url, "save": path, "page": page,
                "spec": renderspec.describe(spec)}

    # --- checkout surface --------------------------------------------------

    def checkout(self, save: str, author: str) -> dict:
        handle = self.checkouts.checkout(save, author=author)
        return {
            "handle": handle.id,
            "save": handle.save,
            "base_rev": handle.base_rev,
            "path": str(self.checkouts.file_path(handle.id)),
            "url": self.url(handle.save, handle=handle.id)["url"],
            "note": "edit via `drawio edit`, or by hand at `path`; then merge",
        }

    def edit(self, handle: str, operations: list[dict]) -> dict:
        """Apply operations to a checkout. All-or-nothing."""
        state = self.checkouts.status(handle)
        if state["conflict_markers"]:
            raise Conflicted(
                f"checkout {handle} has {state['conflict_markers']} unresolved "
                f"conflict(s); edit {state['path']} and call resolve first"
            )
        result = ops.apply(self.checkouts.file_path(handle), operations)
        result["handle"] = handle
        result["save"] = state["save"]
        return result

    def externalize(self, handle: str, roots: list[str]) -> dict:
        """Rewrite a checkout's embedded images as ``/files/…`` references.

        Goes through a checkout like any other edit, so the change lands via
        the normal merge contract and is reviewable in the editor first —
        which matters, because this touches every image cell in the document.
        """
        from . import externalize as ext

        state = self.checkouts.status(handle)
        path = self.checkouts.file_path(handle)
        rewritten, report = ext.externalize(path.read_text(encoding="utf-8"), roots)
        if report["converted"]:
            path.write_text(xmlmodel.normalize(rewritten), encoding="utf-8")
        report["handle"] = handle
        report["save"] = state["save"]
        report["path"] = str(path)
        return report

    def path(self, handle: str) -> dict:
        state = self.checkouts.status(handle)
        return {"handle": handle, "save": state["save"], "path": state["path"]}

    def status(self, handle: str) -> dict:
        return self.checkouts.status(handle)

    def update(self, handle: str) -> dict:
        result = self.checkouts.update(handle)
        if result.get("conflicts"):
            result["how_to_resolve"] = (
                "Open the file at `path`, remove the <<<<<<< / ======= / >>>>>>> "
                "markers keeping the content you want, then call `drawio resolve`. "
                "`merge` refuses while markers remain."
            )
        return result

    def resolve(self, handle: str) -> dict:
        return self.checkouts.resolve(handle)

    def discard(self, handle: str) -> dict:
        return self.checkouts.discard(handle)

    async def merge(self, handle: str, label: str | None = None,
                    keep: bool = False) -> dict:
        """Land a checkout, coordinating with any live editor tabs.

        Ordering, which is the whole point: flush the tabs → confirm the tip
        did not move → land → release → push the result back. Without the
        flush, an in-flight autosave carrying a *pre-merge* snapshot could land
        immediately after and silently revert everything.

        Drift that already existed is **refused**, before anything is touched:
        reconciliation belongs in :meth:`update`, where the agent chose the
        moment and can render the result to check it. Only drift the flush
        itself produced is folded in here — that work is by definition newer
        than anything the agent could have seen, and refusing it would let an
        actively-typing person starve an agent forever, since a live tab moves
        the tip every couple of seconds.
        """
        state = self.checkouts.status(handle)
        save = state["save"]

        if state["behind"]:
            raise Behind(
                f"{save} has moved {state['behind']} revision(s) since this "
                "checkout was taken; call update first, check the result, then "
                "merge"
            )

        # Two distinct guards, and conflating them was a bug: the *lock*
        # serializes merges against each other, while the *lease* blocks
        # browser autosaves. The lease must not be held across the flush —
        # the flush exists precisely to let tabs write, so holding it there
        # would reject the saves we just asked for.
        lock = self._merge_locks.setdefault(save, asyncio.Lock())
        async with lock:
            await self._flush_live_tabs(save)

            lease = self._leases.setdefault(save, Lease())
            lease.acquire(handle)
            try:
                try:
                    return self.checkouts.merge(handle, label=label, keep=keep)
                except Behind:
                    # Only reachable when the flush we just triggered committed
                    # something. Fold it in and retry once — the lease is held
                    # now, so nothing else can move underneath us.
                    result = self.checkouts.update(handle)
                    if result.get("conflicts"):
                        raise Conflicted(
                            f"{save} changed while merging and the changes "
                            f"conflict ({result['conflicts']}); resolve at "
                            f"{state['path']}, then merge again"
                        ) from None
                    return self.checkouts.merge(handle, label=label, keep=keep)
            finally:
                lease.release(handle)
                await self._push_to_live_tabs(save)

    # --- live tab coordination --------------------------------------------

    def lease_held(self, save: str) -> bool:
        """Whether a merge currently owns the write lease for this diagram.

        The editor session layer consults this before accepting an autosave;
        a tab told to hold keeps its edits in memory and retries.
        """
        lease = self._leases.get(save)
        return bool(lease and lease.held())

    def note_tab(self, save: str, tab_id: str) -> None:
        self.live_tabs.setdefault(save, {})[tab_id] = time.time()

    def drop_tab(self, save: str, tab_id: str) -> None:
        self.live_tabs.get(save, {}).pop(tab_id, None)

    def note_flush_ack(self, save: str, tab_id: str) -> None:
        self._flush_acks.setdefault(save, set()).add(tab_id)

    async def _flush_live_tabs(self, save: str) -> None:
        """Ask every open tab to save now, and wait for acknowledgement.

        Best-effort by design: a tab that has gone away without closing its
        socket must not be able to block a merge, so this returns on timeout
        rather than raising. The tip check inside
        :meth:`awm.drawio.checkout.Checkouts.merge` is the real guard — the
        flush only shrinks the window it has to guard against.
        """
        tabs = set(self.live_tabs.get(save, {}))
        if not tabs or self._emit is None:
            return
        self._flush_acks[save] = set()
        await self._emit(self.topic_of(save), {"type": "flush", "reason": "merge"})

        deadline = time.time() + FLUSH_TIMEOUT_S
        while time.time() < deadline:
            if tabs <= self._flush_acks.get(save, set()):
                return
            await asyncio.sleep(0.05)
        missing = tabs - self._flush_acks.get(save, set())
        log.warning("merge on %s: %d tab(s) did not acknowledge flush: %s",
                    save, len(missing), ", ".join(sorted(missing)))

    async def _push_to_live_tabs(self, save: str) -> None:
        """Tell tabs the diagram moved so they reload without a manual refresh."""
        if self._emit is None or not self.live_tabs.get(save):
            return
        await self._emit(self.topic_of(save), {
            "type": "push", "rev": self.store.head_rev(save),
        })

    # --- browser saves -----------------------------------------------------

    def save_from_editor(self, save: str, xml: str, base_rev: str | None,
                         tab_id: str = "tab", handle: str | None = None) -> dict:
        """Accept (or reject) an autosave from a browser tab.

        A save carries the revision the tab was showing. If the diagram has
        moved on since, the save is **rejected** rather than applied — this is
        the fix for the failure that cost the most work in the prototype, where
        a tab left open across a scripted rebuild autosaved its stale in-memory
        model back over the new file and took one page from 47 cells to 2. The
        tab reconciles and retries; it does not get to overwrite blind.

        A tab open on a *checkout* gets the same protection, keyed on the
        working copy's content hash rather than a commit sha — an agent running
        ``edit`` against a checkout somebody has open in the browser is the same
        race in miniature, and deserves the same refusal.
        """
        if handle:
            return self._save_to_checkout(handle, xml, base_rev, tab_id)

        path = store_mod.normalize_save_path(save)
        self.note_tab(path, tab_id)

        if self.lease_held(path):
            return {"save": path, "accepted": False, "reason": "lease",
                    "retry_after": 1.0,
                    "note": "a merge is landing; hold your edits and retry"}

        live_rev = self.store.head_rev(path)
        if base_rev and live_rev and base_rev != live_rev:
            return {"save": path, "accepted": False, "reason": "stale",
                    "rev": live_rev,
                    "note": "the diagram moved since you loaded it; reconcile "
                            "against the current revision and retry"}

        author = f"user:{self.user}/editor:{tab_id}" if self.user else f"editor:{tab_id}"
        result = self.store.write(path, xml, author=author)
        result["accepted"] = True
        return result

    def _save_to_checkout(self, handle: str, xml: str, base_rev: str | None,
                          tab_id: str) -> dict:
        state = self.checkouts.status(handle)
        if base_rev and base_rev != state["content_rev"]:
            return {"handle": handle, "save": state["save"], "accepted": False,
                    "reason": "stale", "rev": state["content_rev"],
                    "note": "this checkout changed since you loaded it "
                            "(an agent edited it); reload to see the current "
                            "working copy"}
        canonical = xmlmodel.normalize(xml)
        path = self.checkouts.file_path(handle)
        changed = path.read_text(encoding="utf-8") != canonical
        if changed:
            path.write_text(canonical, encoding="utf-8")
        return {"handle": handle, "save": state["save"], "accepted": True,
                "changed": changed, "rev": self.checkouts.content_rev(handle)}

    # --- image references --------------------------------------------------

    def check(self, save: str) -> dict:
        """Report every image reference that will not render.

        A ``/files/…`` reference resolves through fileviewer's mount, whose mask
        is a denylist that 404s exactly like a missing file. So "the path exists"
        is not enough — a figure stored under a masked directory is invisible in
        the browser and blank in an export, with nothing logged anywhere. Both
        cases are reported here, separately, because the fixes differ: one is a
        missing render, the other a file in the wrong place.

        Placed page views are checked too, against the same pattern the exporter
        inlines with — a checker that only looked for ``/files`` would let a
        renamed source page through, and the ``export`` gate that consults this
        would pass an export with a hole in it. Nothing is rendered here: this
        stays a cheap pre-flight, so it verifies that the diagram, the page and
        the parameters resolve, and leaves the render to the caller.
        """
        from . import export as export_mod
        from . import view as view_mod
        from .autopublish import AutoPublishError, page_index

        text = self.store.read(save)
        problems, checked = [], []
        for match in export_mod.REFERENCE_PATTERN.finditer(text):
            if match.group("file") is not None:
                target = Path(match.group("file"))
                checked.append(str(target))
                if not target.exists():
                    problems.append({"path": str(target), "problem": "missing",
                                     "fix": "render the image, or repoint the cell"})
                elif _masked(target):
                    problems.append({
                        "path": str(target), "problem": "masked",
                        "fix": "fileviewer hides this path, so it 404s like a "
                               "missing file; move the image outside the mask",
                    })
                continue

            url = export_mod.unescape_amp(match.group("view"))
            checked.append(url)
            try:
                rel, query = view_mod.split_view_url(url)
                renderspec.from_query(query)
                source, page = view_mod.resolve_target(self.store, rel)
                page_index(self.store.read(source), page)
            except (view_mod.ViewError, renderspec.SpecError,
                    AutoPublishError) as exc:
                problems.append({
                    "path": url, "problem": "view",
                    "fix": f"{exc} — repoint the cell with `drawio view_url`, "
                           "or restore the page it names",
                })
        return {
            "save": store_mod.normalize_save_path(save),
            "references": len(checked),
            "problems": problems,
            "ok": not problems,
        }


def _masked(path: Path) -> bool:
    """Whether fileviewer's mask hides this path (so it would 404).

    Imported lazily and tolerantly: fileviewer is a sibling dist, and a
    ``check`` that crashes because a neighbour is unavailable is worse than one
    that reports only what it can verify.
    """
    try:
        from awm.fileviewer.server import load_mask
        import fnmatch

        resolved = path.resolve().as_posix()
        hidden = False
        for glob in load_mask():
            negate = glob.startswith("!")
            pattern = glob[1:] if negate else glob
            if fnmatch.fnmatch(resolved, pattern) or fnmatch.fnmatch(
                    resolved, "/" + pattern.lstrip("/")):
                hidden = not negate  # last match wins, gitignore-style
        return hidden
    except Exception as exc:  # noqa: BLE001
        # "Not masked" is the tolerant answer but it is also possibly a wrong
        # one, so it does not get to be invisible — this is precisely the
        # silent class of failure `check` exists to surface.
        log.warning("could not consult fileviewer's mask for %s (%s); "
                    "reporting it as visible, which may be wrong", path, exc)
        return False
