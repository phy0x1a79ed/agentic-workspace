"""Autopublish links: a diagram page kept rendered to a file, one way, forever.

``export`` is a pull — somebody has to ask. An autopublish link is the standing
version of the same thing: *this diagram, this page, rendered to this path, kept
current*. Every accepted write to the diagram re-renders every link on it, so a
poster, a paper figure or a README embed stops needing anyone to remember.

Four properties are the whole design, and each one is a failure this avoids.

**One way, always.** Nothing reads the target back. The diagram is the source
and the file is a copy; a link can never carry an edit backwards into the store.

**Replace, never write in place.** The render goes to a sibling ``.tmp`` and is
``os.replace``-d onto the target. A LaTeX build or a file watcher reading at the
wrong moment sees the old file or the new one — never half of either.

**Last good stays.** A render that fails — broken ``/files`` references, the
export container down, the named page gone — leaves the published file exactly
as it was and writes the reason to the service log. A stale-but-correct figure
is recoverable; a blank one that looks finished is not. Nothing about the
failure is recorded on the link itself: links are configuration, not health.

**The destination owns the link's life.** If the target's parent directory has
gone away, the link is deleted rather than recreating it. A directory that
vanished was deleted on purpose, and silently resurrecting it — with a figure
inside — is not something a publishing side-effect gets to decide.

Pages are addressed by *name*, not index. Reordering tabs in the editor shifts
every index, and a link that quietly starts publishing a different page is worse
than one that stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import renderspec, xmlmodel
from .store import Store, normalize_save_path

log = logging.getLogger("awm.drawio.autopublish")

#: How quiet a diagram must go before its links render. A browser tab autosaves
#: every couple of seconds and each render is a round trip to the export
#: container, so without this a minute of editing would queue thirty renders of
#: intermediate states nobody asked for. The window *restarts* on every save,
#: which is what makes an editing burst cost one render rather than one per save.
DEBOUNCE_SECONDS = 3.0

#: …but a diagram somebody is continuously editing never goes quiet, and a link
#: that only updates when its author stops working is not "continuous". This is
#: the ceiling: however busy the diagram, its links publish at least this often.
MAX_DEFER_SECONDS = 30.0

#: Formats a link may publish. Same set :mod:`awm.drawio.export` renders.
FORMATS = ("svg", "pdf", "png", "jpg")

REGISTRY_FILENAME = "autopublish.json"


class AutoPublishError(RuntimeError):
    """A link could not be created or found."""


@dataclass
class Link:
    """One standing diagram → file link.

    Deliberately carries no error or health field. A link says what should
    happen; whether the last attempt worked is a log line, not configuration.
    """

    id: str
    save: str
    target: str
    format: str = "svg"
    page: str | None = None
    scale: float = 1.0
    author: str = "agent"
    created_at: int = 0
    last_rev: str | None = None
    last_published_at: int | None = None
    #: The view URL's parameters, so a published file is definitionally the
    #: bytes that link serves rather than a second thing that happens to agree.
    #: Empty defaults, so every record written before they existed still loads.
    swaps: list[str] = field(default_factory=list)
    crop: str | None = None

    def spec(self) -> renderspec.RenderSpec:
        return renderspec.RenderSpec(
            scale=self.scale,
            swaps=renderspec.parse_swaps(self.swaps or ()),
            crop=self.crop or None,
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Registry:
    """The link list, persisted as one JSON file.

    A JSON file rather than a SQLite table because this service has no database
    and a handful of links does not justify introducing one — the filesystem is
    already where every other piece of drawio state lives.

    Every mutation happens on the event loop thread; the blocking render runs in
    an executor and returns its result rather than writing here, so there is no
    lock to get wrong.
    """

    path: Path
    links: dict[str, Link] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Registry":
        registry = cls(Path(path))
        if not registry.path.is_file():
            return registry
        try:
            raw = json.loads(registry.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt registry must not stop the service booting; losing the
            # links is recoverable, refusing to start is not.
            log.error("autopublish registry at %s is unreadable (%s); "
                      "starting with no links", registry.path, exc)
            return registry
        for record in raw.get("links", []):
            try:
                link = Link(**record)
            except TypeError as exc:
                log.warning("skipping malformed autopublish record %r: %s",
                            record, exc)
                continue
            registry.links[link.id] = link
        return registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"links": [link.as_dict() for link in self.links.values()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self, save: str | None = None) -> list[Link]:
        wanted = normalize_save_path(save) if save else None
        return sorted(
            (l for l in self.links.values() if wanted is None or l.save == wanted),
            key=lambda l: (l.save, l.target),
        )

    def get(self, link_id: str) -> Link:
        try:
            return self.links[link_id]
        except KeyError:
            raise AutoPublishError(f"no autopublish link {link_id!r}") from None

    def by_target(self, target: Path) -> Link | None:
        for link in self.links.values():
            if Path(link.target) == target:
                return link
        return None


def default_registry_path() -> Path:
    """``<AWM_DIR>/services/drawio/autopublish.json``.

    Sibling to the ``diagrams/`` store and ``checkouts/`` roots, resolved the
    same way and overridable with ``AWM_DRAWIO_AUTOPUBLISH`` so tests and dev
    sandboxes never publish out of prod's registry.
    """
    override = os.environ.get("AWM_DRAWIO_AUTOPUBLISH")
    if override:
        return Path(override).expanduser()
    from awm.persistence.databases import service_db_path

    return service_db_path("drawio").parent / REGISTRY_FILENAME


def page_index(xml, name: str | None) -> int | None:
    """Resolve a page *name* to the index the export server wants.

    ``None`` name means "the whole document, let the exporter decide", which for
    a single-page diagram is the only page anyway. A named page that no longer
    exists raises, because publishing the wrong page silently is the failure
    name-addressing exists to prevent.

    ``xml`` may be an already-parsed ``mxfile``, for a caller that goes on to
    use the same tree.
    """
    if name is None:
        return None
    pages = xmlmodel.page_summaries(xmlmodel.as_tree(xml))
    for index, page in enumerate(pages):
        if page.get("name") == name:
            return index
    known = ", ".join(repr(p.get("name")) for p in pages) or "none"
    raise AutoPublishError(f"no page named {name!r} in this diagram (has: {known})")


class AutoPublisher:
    """Owns the links, the debounce loop, and the render-and-replace step.

    ``render`` is injected so tests can drive the whole machine without the
    export container — it is :func:`awm.drawio.export.render` in production.
    """

    def __init__(self, store: Store, registry_path: Path | None = None,
                 render: Callable[..., tuple[bytes, list[str]]] | None = None):
        self.store = store
        self.registry = Registry.load(registry_path or default_registry_path())
        self._render = render
        #: save -> (first dirtied at, last dirtied at), both monotonic. The
        #: pair is what lets the loop debounce on quiet without letting a
        #: continuously-edited diagram defer forever.
        self._dirty: dict[str, tuple[float, float]] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Test seam: incremented every time a render actually runs, which is
        #: how debounce coalescing is asserted.
        self.renders = 0

    # --- the render step ---------------------------------------------------

    def _renderer(self) -> Callable[..., tuple[bytes, list[str]]]:
        if self._render is not None:
            return self._render
        from . import export as export_mod

        return export_mod.render

    def _plan(self, link: Link, force: bool) -> tuple[str, object]:
        """Decide what to do about one link, cheaply, before rendering.

        Returns ``(action, detail)`` where action is ``delete`` / ``skip`` /
        ``render``. Split out from the render itself so every registry decision
        happens on the loop thread and the blocking part is pure.
        """
        target = Path(link.target)
        if not target.parent.is_dir():
            return "delete", "the target's parent folder no longer exists"
        if not self.store.exists(link.save):
            return "delete", "the diagram no longer exists"

        head = self.store.head_rev(link.save)
        if not force and head == link.last_rev and target.is_file():
            return "skip", "already current"

        xml = self.store.read(link.save)
        try:
            index = page_index(xml, link.page)
        except AutoPublishError as exc:
            return "skip", str(exc)
        return "render", (xml, index, head)

    def _render_to_target(self, link: Link, xml: str, index: int | None) -> int:
        """Render and atomically replace the target. Runs in an executor.

        Blocking throughout — an HTTP round trip to the export container plus
        ``docker`` subprocesses — which is exactly why it must not touch the
        registry or run on the loop.
        """
        spec = link.spec()
        data, problems = self._renderer()(
            xml, link.format, page=index, scale=spec.scale,
            swaps=spec.swaps, crop=spec.crop)
        if problems:
            raise AutoPublishError(
                "image reference(s) could not be inlined: " + "; ".join(problems))

        target = Path(link.target)
        tmp = target.with_name(f".{target.name}.autopublish.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return len(data)

    async def service(self, link: Link, force: bool = False) -> dict:
        """Bring one link up to date. Never raises — outcomes are returned.

        Every path through here that is not a clean publish leaves the target
        file untouched, so the previously published render survives any failure.
        """
        action, detail = self._plan(link, force)

        if action == "delete":
            self.registry.links.pop(link.id, None)
            self.registry.save()
            log.warning("autopublish %s dropped: %s (%s -> %s)",
                        link.id, detail, link.save, link.target)
            return {"id": link.id, "save": link.save, "target": link.target,
                    "published": False, "deleted": True, "reason": str(detail)}

        if action == "skip":
            if detail != "already current":
                log.warning("autopublish %s not updated: %s (%s -> %s)",
                            link.id, detail, link.save, link.target)
            return {"id": link.id, "save": link.save, "target": link.target,
                    "published": False, "reason": str(detail)}

        xml, index, head = detail  # type: ignore[misc]
        loop = asyncio.get_running_loop()
        try:
            written = await loop.run_in_executor(
                None, self._render_to_target, link, xml, index)
        except Exception as exc:  # noqa: BLE001 — every failure keeps last good
            log.warning("autopublish %s failed, keeping the last published "
                        "file: %s (%s%s -> %s)", link.id, exc, link.save,
                        f" page {link.page!r}" if link.page else "", link.target)
            return {"id": link.id, "save": link.save, "target": link.target,
                    "published": False, "reason": str(exc)}

        self.renders += 1
        link.last_rev = head
        link.last_published_at = int(time.time())
        self.registry.save()
        log.info("autopublish %s: %s%s -> %s (%s, %d bytes, rev %s)",
                 link.id, link.save, f" page {link.page!r}" if link.page else "",
                 link.target, link.format, written, (head or "?")[:8])
        return {"id": link.id, "save": link.save, "target": link.target,
                "published": True, "bytes": written, "rev": head,
                "format": link.format, "page": link.page}

    # --- the verbs ---------------------------------------------------------

    async def create(self, save: str, target: str, page: str | None = None,
                     fmt: str = "svg", scale: float = 1.0,
                     author: str = "agent", swaps: list[str] | None = None,
                     crop: str | None = None) -> dict:
        """Register a link and publish it once, synchronously.

        The first render happens inline so the caller finds out immediately that
        the container is down or a reference is broken — every later render is
        a background side-effect nobody is watching.

        ``swaps`` and ``crop`` are the view URL's parameters, taken through the
        same parser, so a link and a placed image of the same page cannot drift
        into meaning different things. Several links off one page with different
        parameters is just several files.
        """
        fmt = (fmt or "svg").lower()
        if fmt not in FORMATS:
            raise AutoPublishError(
                f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")

        try:
            parsed_swaps = renderspec.parse_swaps(swaps or ())
        except renderspec.SpecError as exc:
            raise AutoPublishError(str(exc)) from None
        if crop and fmt != "svg":
            raise AutoPublishError(
                "crop needs an exact render of one shape, which only the SVG "
                f"path can do; a {fmt} link goes through the export container")

        path = normalize_save_path(save)
        if not self.store.exists(path):
            raise AutoPublishError(f"no diagram at {path!r}")

        destination = Path(target).expanduser()
        if not destination.is_absolute():
            raise AutoPublishError(
                f"target must be an absolute path: {target!r}")
        if destination.suffix.lower() != f".{fmt}":
            # A link is a standing overwrite of a path, not a one-shot like
            # export's `out`. Requiring the extension to match is what keeps a
            # fat-fingered target from quietly eating an unrelated file forever.
            raise AutoPublishError(
                f"target must end in .{fmt} for a {fmt} link: {target!r}")
        if not destination.parent.is_dir():
            raise AutoPublishError(
                f"the target's parent folder does not exist: "
                f"{destination.parent} (autopublish never creates it)")

        clash = self.registry.by_target(destination)
        if clash is not None:
            raise AutoPublishError(
                f"{destination} is already published from {clash.save!r} "
                f"(link {clash.id}); stop that link first"
            )

        # Fail before registering rather than after: a link that can never
        # resolve its page — or crop to a shape that is not there — is a
        # configuration error, not a runtime one.
        xml = self.store.read(path)
        index = page_index(xml, page) if page is not None else None
        if crop:
            try:
                renderspec.prepare_crop(xml, index, crop)
            except (renderspec.CropNotFound, xmlmodel.MalformedDiagram) as exc:
                raise AutoPublishError(str(exc)) from None

        link = Link(
            id=uuid.uuid4().hex[:12], save=path, target=str(destination),
            format=fmt, page=page, scale=float(scale), author=author,
            created_at=int(time.time()),
            swaps=[f"{a}:{b}" for a, b in parsed_swaps], crop=crop or None,
        )
        self.registry.links[link.id] = link
        self.registry.save()
        log.info("autopublish %s created: %s%s -> %s (%s) by %s",
                 link.id, link.save, f" page {page!r}" if page else "",
                 link.target, fmt, author)

        result = await self.service(link, force=True)
        return {**link.as_dict(), "first_publish": result}

    def list(self, save: str | None = None) -> dict:
        links = self.registry.list(save)
        return {"links": [l.as_dict() for l in links], "count": len(links)}

    def stop(self, link_id: str) -> dict:
        """Delete a link. The published file stays where it is.

        Deleting something a build may be consuming is not a decision that
        belongs to "stop updating this".
        """
        link = self.registry.get(link_id)
        self.registry.links.pop(link_id, None)
        self.registry.save()
        log.info("autopublish %s stopped: %s -> %s (file left in place)",
                 link_id, link.save, link.target)
        return {**link.as_dict(), "stopped": True,
                "note": "the published file was left on disk"}

    async def now(self, link_id: str | None = None,
                  save: str | None = None) -> dict:
        """Force a re-render. The on-demand answer to "why is my file stale".

        Nothing about a failure is stored on the link, so this — plus the
        service log — is how you find out what a link is unhappy about.
        """
        if link_id:
            links = [self.registry.get(link_id)]
        elif save:
            links = self.registry.list(save)
            if not links:
                raise AutoPublishError(
                    f"no autopublish links for {normalize_save_path(save)!r}")
        else:
            links = self.registry.list()
        results = [await self.service(link, force=True) for link in links]
        return {"results": results, "count": len(results),
                "published": sum(1 for r in results if r["published"])}

    # --- the trigger -------------------------------------------------------

    def notify(self, save: str, rev: str | None = None) -> None:
        """A diagram changed. Called from the store's commit hook.

        Cheap and non-blocking on purpose: this runs inside whatever accepted
        the write — a browser autosave, an agent's merge — and none of them
        should wait on a render.
        """
        if not any(link.save == save for link in self.registry.links.values()):
            return
        now = time.monotonic()
        first, _ = self._dirty.get(save, (now, now))
        self._dirty[save] = (first, now)
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop to poke — the work stays in `_dirty` and the next
                # notification that does reach the loop drains it too.
                return
        # The hook may fire off the loop thread (handlers can run in a
        # threadpool), and Event.set is not thread-safe.
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:  # pragma: no cover — loop already closed
            pass

    def attach(self, store: Store) -> None:
        """Wire this publisher to a store's commit hook.

        Captures the loop here rather than waiting for :meth:`run` to start, so
        a write landing in the gap between startup and the loop's first tick
        still wakes it.
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        store.subscribe(self.notify)

    def _ready(self) -> list[str]:
        """Diagrams whose links should render now.

        A diagram is ready once it has been quiet for :data:`DEBOUNCE_SECONDS`
        — so an editing burst costs one render — or once it has been waiting
        :data:`MAX_DEFER_SECONDS`, which is what stops a diagram somebody is
        typing into continuously from deferring its links indefinitely.
        """
        now = time.monotonic()
        return sorted(
            save for save, (first, last) in self._dirty.items()
            if now - last >= DEBOUNCE_SECONDS or now - first >= MAX_DEFER_SECONDS
        )

    async def run(self) -> None:
        """The debounce loop. Started as a task; never returns."""
        self._loop = asyncio.get_running_loop()
        while True:
            if not self._dirty:
                await self._wake.wait()
                self._wake.clear()
                continue
            # Poll rather than sleep the whole window: each new save pushes the
            # deadline out, so the wait cannot be computed once up front.
            await asyncio.sleep(min(0.25, DEBOUNCE_SECONDS / 2))
            for save in self._ready():
                self._dirty.pop(save, None)
                for link in self.registry.list(save):
                    try:
                        await self.service(link)
                    except Exception:  # noqa: BLE001
                        # One bad link must never take the loop down and stop
                        # every other link on the host.
                        log.exception("autopublish %s raised unexpectedly",
                                      link.id)

    async def reconcile(self) -> dict:
        """Catch every link up at boot.

        Drops links whose destination or diagram went away while the service was
        down, and re-renders anything whose diagram moved or whose file was
        deleted underneath it. Without this, an edit made while the service was
        stopped would never reach a published file.
        """
        results = []
        for link in self.registry.list():
            try:
                results.append(await self.service(link))
            except Exception:  # noqa: BLE001
                log.exception("autopublish %s raised during reconcile", link.id)
        published = sum(1 for r in results if r.get("published"))
        dropped = sum(1 for r in results if r.get("deleted"))
        if results:
            log.info("autopublish reconcile: %d link(s), %d republished, "
                     "%d dropped", len(results), published, dropped)
        return {"links": len(results), "published": published,
                "dropped": dropped, "results": results}

    def status(self) -> dict:
        return {"registry": str(self.registry.path),
                "links": len(self.registry.links)}
