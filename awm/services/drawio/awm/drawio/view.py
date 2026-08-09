"""A page's live SVG at a stable URL — drawio's own render, on demand, cached.

``export`` writes a file once; ``autopublish`` keeps a file rendered. This is the
third shape: *a URL that returns the render*, so a page can be dropped into
another diagram straight from drawio's own **Insert → Image → URL** and behave
like any placed image — movable, selectable, and (with the client hook in
:mod:`patches/PreConfig.js`) kept current while the consumer is open.

**Why a loopback listener and a ``kind=url`` mount.** A service's browser surface
is ``POST /svc/<name>/fn/<fn>`` — there is no GET-to-verb, and drawio's
image-from-URL is a plain GET expecting ``image/svg+xml``. A ``kind=static`` mount
cannot render on demand. So this runs a tiny HTTP listener bound to loopback and
registers it as a ``kind=url`` record at ``/drawio-app/view``; the gateway's
longest-prefix routing lets that coexist with the ``/drawio-app`` static editor
mount, and forwards the full path through (it does *not* strip the prefix), which
is why the handler parses relative to ``/drawio-app/view/``.

**The cache key is content, not a revision.** A page is keyed by the SHA-256 of
its *inlined* XML — the document with every ``/files/…`` reference already
replaced by the file's bytes. That busts the cache on a diagram edit **and** on a
changed referenced image (a re-rendered molecule that never commits the diagram),
which the biomass map depends on. An unchanged page therefore keeps its cache
across an unrelated commit, and the ``ETag`` lets the client's change-event
re-fetch collapse to a cheap ``304`` when nothing actually moved.

**One page, many variants.** The query string can recolour and crop the render
(:mod:`awm.drawio.renderspec` owns that grammar), so one source page serves
every placement instead of a near-duplicate page per colour. Each variant caches
in its own subdirectory, which is what makes the version cap apply per variant
rather than letting three colours thrash one five-slot cache. The
un-parameterised render keeps its original path and key, so deploying variants
throws nothing already rendered away. All variants of a page share one change
topic and therefore refresh together, which is correct: they have one source.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
import websockets

from . import export as export_mod
from . import renderspec
from . import xmlmodel
from .store import Store, StoreError, UnknownDiagram, normalize_save_path

log = logging.getLogger("awm.drawio.view")

SERVICE_NAME = os.environ.get("AWM_SERVICE_NAME", "drawio")
#: A separate prefix, nested under the editor's ``/drawio-app`` static mount.
#: Longest-prefix routing in the gateway resolves ``/drawio-app/view/…`` here and
#: everything else under ``/drawio-app`` to the editor bytes. Defined in
#: :mod:`awm.drawio.renderspec` so ``export`` can recognise a view reference
#: without importing this module, and re-exported here where callers expect it.
VIEW_PREFIX = renderspec.VIEW_PREFIX
#: Registered under its own name so it never collides with the ``drawio`` service
#: record or the ``drawio`` static mount (the registry keys on ``(kind, name)``).
MOUNT_NAME = os.environ.get("DRAWIO_VIEW_NAME", "drawio-view")

#: Cache dir name for a page-omitted (whole-document) view — a segment a real
#: page name can never collide with, since names are percent-encoded.
WHOLE_DOC = "__whole__"

#: How many rendered versions to keep per page. Content changes mint a new hash,
#: so old renders would otherwise pile up forever; the newest few are enough for
#: a just-served ETag to still validate.
MAX_VERSIONS_PER_PAGE = 5

#: How many *variants* of a page to keep. The query space is caller-controlled
#: and unbounded, and nothing else would ever reclaim an abandoned colour.
MAX_VARIANTS_PER_PAGE = 12


def default_cache_dir() -> Path:
    """``<AWM_DIR>/services/drawio/viewcache`` — sibling to the store.

    Overridable with ``AWM_DRAWIO_VIEWCACHE`` so tests and dev sandboxes never
    share prod's rendered pages.
    """
    override = os.environ.get("AWM_DRAWIO_VIEWCACHE")
    if override:
        return Path(override).expanduser()
    from awm.persistence.databases import service_db_path

    return service_db_path("drawio").parent / "viewcache"


class ViewError(Exception):
    """A view request could not be resolved. ``status`` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _exists(store: Store, save: str) -> bool:
    try:
        return store.exists(save)
    except StoreError:
        return False


def resolve_target(store: Store, rel_path: str) -> tuple[str, str | None]:
    """Map the path after the prefix to ``(save, page_name | None)``.

    The last segment is the page name (a trailing ``.svg`` is stripped); the
    rest is the diagram path. If that does not name a diagram, the *whole*
    remainder is tried as a page-omitted request for the whole document — so
    ``view/a/b`` is diagram ``a`` page ``b`` when ``a`` is a diagram, else
    diagram ``a/b``. The save-plus-page reading wins, and the two can never both
    resolve: a store path always ends in ``.drawio``, so the fallback would need
    that same name to be a directory too.

    A module function rather than a method because :meth:`Service.check` has to
    answer the same question about a reference it found in a document, and it
    has a store but no renderer.
    """
    raw = rel_path.strip("/")
    if not raw:
        raise ViewError(404, "no diagram in the view path")
    segments = [unquote(s) for s in raw.split("/") if s]
    if not segments:
        raise ViewError(404, "no diagram in the view path")

    # Strip a cosmetic .svg from whatever ends up being the last segment.
    def _strip_svg(name: str) -> str:
        return name[:-4] if name.lower().endswith(".svg") else name

    # Prefer save = all-but-last, page = last (the documented shape).
    if len(segments) >= 2:
        save = "/".join(segments[:-1])
        page = _strip_svg(segments[-1])
        if _exists(store, save):
            return normalize_save_path(save), page

    # Fall back to the whole remainder as a page-omitted diagram path.
    whole = "/".join(segments[:-1] + [_strip_svg(segments[-1])])
    if _exists(store, whole):
        return normalize_save_path(whole), None

    raise ViewError(404, f"no diagram for view path {raw!r}")


def split_view_url(url: str) -> tuple[str, dict]:
    """``/drawio-app/view/a/b?swap=…`` → ``("/a/b", {"swap": [...]})``.

    Accepts the absolute form too, since a diagram may have been authored with a
    full URL pasted in. Blank values are kept: ``?swap=`` has to reach the
    parser so it can be refused, rather than vanishing on the way.
    """
    parts = urlsplit(url)
    path = parts.path
    marker = VIEW_PREFIX
    index = path.find(marker)
    if index < 0:
        raise ViewError(404, f"{url!r} is not a page-view URL")
    return path[index + len(marker):], parse_qs(parts.query,
                                                keep_blank_values=True)


class Renderer:
    """Resolves ``<save>/<page>`` requests to cached SVG bytes.

    Split out from the HTTP plumbing so it is drivable in a test without a
    socket: ``render`` is injected (it is :func:`awm.drawio.export.render` in
    production) exactly as :class:`awm.drawio.autopublish.AutoPublisher` injects
    it, so neither the export container nor a browser is needed to exercise the
    parsing and caching.
    """

    def __init__(self, store: Store, cache_dir: Path | None = None,
                 render=None):
        self.store = store
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self._render = render or export_mod.render
        #: Bumped whenever a render actually runs — the test seam for asserting
        #: a cache hit did not re-render, mirroring ``AutoPublisher.renders``.
        self.renders = 0

    # -- request parsing ----------------------------------------------------

    def resolve_target(self, rel_path: str) -> tuple[str, str | None]:
        """This renderer's store, resolved by :func:`resolve_target`."""
        return resolve_target(self.store, rel_path)

    # -- cache layout -------------------------------------------------------
    #
    # Path-addressable, not flat: ``viewcache/<enc save>/<enc page>/<hash>.svg``,
    # and ``…/<enc page>/<variant>/<hash>.svg`` once a query asks for something
    # other than the plain render. A per-page directory is what lets a gone page
    # or diagram be pruned by deleting a directory (see
    # :meth:`prune_for_commit`) — the same principle autopublish states as "the
    # destination owns the link's life" — and putting variants one level below
    # it means that pruning keeps working untouched.

    def _save_dir(self, save: str) -> Path:
        return self.cache_dir / quote(save, safe="")

    def _page_dir(self, save: str, page: str | None) -> Path:
        leaf = quote(page, safe="") if page else WHOLE_DOC
        return self._save_dir(save) / leaf

    def _variant_dir(self, save: str, page: str | None,
                     spec: renderspec.RenderSpec) -> Path:
        """Where this variant's renders live.

        The plain render stays exactly where it has always been, so a deploy
        does not orphan a single already-rendered page.
        """
        page_dir = self._page_dir(save, page)
        if spec.is_plain:
            return page_dir
        return page_dir / renderspec.fingerprint(spec)

    # -- rendering + cache --------------------------------------------------

    def _page_index(self, xml: str, name: str | None) -> int | None:
        """Resolve a page name to its export index, or ``None`` for the whole
        document. Reuses the same name→index contract autopublish uses so a
        reordered tab never silently repoints a view."""
        from .autopublish import AutoPublishError, page_index

        try:
            return page_index(xml, name)
        except AutoPublishError as exc:
            raise ViewError(404, str(exc)) from None

    def _content_key(self, inlined: str, index: int | None,
                     spec: renderspec.RenderSpec) -> str:
        """Hash the *specific page's* inlined content (whole doc if page-omitted).

        Keying per page — not per whole document — is what keeps an unchanged
        page's cache valid across a commit that only touched a sibling page.

        The spec's fingerprint joins the hash only when it is not the plain one,
        which keeps every existing cache entry valid across this change. It is
        belt-and-braces given the swapped material already differs, but it is
        what guarantees two variants can never collide on an ETag — and the
        ETag is what the revalidation path turns on.
        """
        material = inlined
        if index is not None:
            try:
                mxfile = xmlmodel.parse(inlined)
                diagrams = mxfile.findall("diagram")
                if 0 <= index < len(diagrams):
                    material = xmlmodel.serialize(diagrams[index])
            except xmlmodel.MalformedDiagram:
                material = inlined  # hash the whole thing rather than crash
        digest = hashlib.sha256()
        digest.update(f"v1|{index}|{spec.scale:.4f}|".encode("utf-8"))
        if not spec.is_plain:
            digest.update(f"{renderspec.fingerprint(spec)}|".encode("utf-8"))
        digest.update(material.encode("utf-8"))
        return digest.hexdigest()

    def resolve_meta(self, rel_path: str) -> tuple[str, str | None, str | None]:
        """Resolve ``(save, page, head_rev)`` without rendering — the cheap HEAD
        answer the consumer client uses to learn which topic to subscribe to,
        free of the save-vs-page path ambiguity a client cannot settle alone."""
        save, page_name = self.resolve_target(rel_path)
        return save, page_name, self.store.head_rev(save)

    def render(self, rel_path: str, spec: renderspec.RenderSpec | None = None,
               rev: str | None = None, resolver=None,
               budget=None) -> "RenderResult":
        """Resolve, render (or hit the cache), and return the SVG + metadata.

        Never returns a half-document: a missing diagram or unknown page raises
        :class:`ViewError` with a 404, which the handler turns into an honest
        error rather than a blank image. An unknown crop frame is a 404 for the
        same reason — a parameter that silently does nothing is worse than one
        that fails.

        The order is: read, resolve the page, transform for the spec, inline,
        then key over that final material. Swapping before inlining means the
        colour rewrite only ever sees ``/files`` references, never a
        percent-encoded payload it might corrupt.
        """
        spec = spec or renderspec.DEFAULT
        save, page_name = resolve_target(self.store, rel_path)
        head_rev = rev or self.store.head_rev(save)
        try:
            xml = self.store.read(save, rev=rev)
        except UnknownDiagram as exc:
            raise ViewError(404, str(exc)) from None
        except StoreError as exc:
            raise ViewError(404, str(exc)) from None

        index = self._page_index(xml, page_name)

        if spec.swaps:
            try:
                xml, hits = renderspec.swap_document(xml, spec.swaps)
            except xmlmodel.CompressedDiagram as exc:
                raise ViewError(
                    422, f"this diagram is compressed, so its colours cannot be "
                         f"swapped: {exc}") from None
            except xmlmodel.MalformedDiagram as exc:
                raise ViewError(422, str(exc)) from None
            if hits == 0:
                # Not an error — a mask may live on one page and not another —
                # but not silent either, since "nothing happened" and "the
                # parameter was ignored" look identical from the outside.
                log.warning("view of %s%s: no colour matched %s", save,
                            f" page {page_name!r}" if page_name else "",
                            renderspec.describe(spec))

        crop_id = None
        if spec.crop:
            try:
                xml, crop_id = renderspec.prepare_crop(xml, index, spec.crop)
            except renderspec.CropNotFound as exc:
                raise ViewError(404, str(exc)) from None
            except xmlmodel.CompressedDiagram as exc:
                raise ViewError(422, str(exc)) from None

        # Inline once here (for the content key) and hand the already-inlined
        # document to render with inline=False, so the /files bytes are read a
        # single time per request rather than twice. This also strictly precedes
        # the render call, which is what keeps a nested page view from
        # re-entering the browser's non-reentrant lock.
        if resolver is None:
            resolver = ViewResolver(self, seen=frozenset({(save, page_name)}))
        inlined, problems = export_mod.inline_images(
            xml, swaps=spec.swaps, resolver=resolver, budget=budget)
        key = self._content_key(inlined, index, spec)

        variant_dir = self._variant_dir(save, page_name, spec)
        cache_file = variant_dir / f"{key}.svg"
        if cache_file.is_file():
            return RenderResult(cache_file.read_bytes(), key, save, page_name,
                                head_rev, problems, cached=True)

        data, _ = self._render(inlined, "svg", inline=False, page=index,
                               scale=spec.scale, crop_id=crop_id)
        self.renders += 1
        variant_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_name(f".{cache_file.name}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, cache_file)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._cap_versions(variant_dir)
        if not spec.is_plain:
            self._cap_variants(self._page_dir(save, page_name))
        return RenderResult(data, key, save, page_name, head_rev, problems,
                            cached=False)

    def _cap_versions(self, variant_dir: Path) -> None:
        """Keep only the newest :data:`MAX_VERSIONS_PER_PAGE` renders in a dir.

        Per *variant*, not per page — three colours of one plasmid would
        otherwise share five slots and evict each other on every edit.
        """
        try:
            svgs = sorted(variant_dir.glob("*.svg"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
        except OSError:
            return
        for stale in svgs[MAX_VERSIONS_PER_PAGE:]:
            stale.unlink(missing_ok=True)

    def _cap_variants(self, page_dir: Path) -> None:
        """Drop the least recently rendered variants beyond the cap.

        The plain render is not a variant directory, so it is never a candidate
        — it lives as loose ``*.svg`` files in the page directory.
        """
        try:
            dirs = sorted((p for p in page_dir.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for stale in dirs[MAX_VARIANTS_PER_PAGE:]:
            shutil.rmtree(stale, ignore_errors=True)

    # -- pruning (a gone page/diagram owns its cache) -----------------------

    def prune_for_commit(self, save: str, rev: str | None = None) -> None:
        """Drop cache a commit orphaned: a removed diagram or a gone page name.

        Called from the store's commit hook, so a page rename (old name → gone)
        and a diagram removal both reach it. Never raises — a stale SVG left
        behind is harmless, but a pruning error must not fail the write.
        """
        try:
            save = normalize_save_path(save)
            save_dir = self._save_dir(save)
            if not save_dir.is_dir():
                return
            if not self.store.exists(save):
                shutil.rmtree(save_dir, ignore_errors=True)
                return
            xml = self.store.read(save)
            pages = xmlmodel.page_summaries(xmlmodel.parse(xml))
            live = {quote(p["name"], safe="") for p in pages if p.get("name")}
            live.add(WHOLE_DOC)  # the page-omitted render is always valid
            for page_dir in save_dir.iterdir():
                if page_dir.is_dir() and page_dir.name not in live:
                    shutil.rmtree(page_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 — pruning never fails a commit
            log.warning("view cache prune for %s failed: %s", save, exc)


class ViewResolver:
    """Turns a ``/drawio-app/view/…`` reference into rendered SVG bytes.

    This is what makes an exported figure portable. A placed page view is an
    origin-relative URL, so a document that keeps one is only a picture on this
    host; the exporter asks this to render the page instead and embeds the
    result. It is handed to :func:`awm.drawio.export.inline_images` by
    injection, which is how ``export`` stays free of an import back into the
    view layer that imports it.

    The recursion state rides on the resolver rather than on every signature it
    passes through. Cycles are keyed on ``(save, page)`` and deliberately
    exclude the query — a page embedding *itself* at a different scale is still
    a cycle — and the check runs after path resolution but before any render, so
    detecting one costs no browser time.
    """

    def __init__(self, renderer: "Renderer", depth: int = 0,
                 seen: frozenset = frozenset()):
        self.renderer = renderer
        self.depth = depth
        self.seen = seen

    def __call__(self, url: str, budget) -> tuple[bytes, list[str]]:
        rel, query = split_view_url(url)
        spec = renderspec.from_query(query)
        rev = (query.get("rev") or [None])[0] or None
        save, page = resolve_target(self.renderer.store, rel)

        if (save, page) in self.seen:
            raise export_mod.ExportError(
                f"{save}{f' page {page!r}' if page else ''} embeds itself; "
                "flatten one of the placements")
        if self.depth >= export_mod.MAX_VIEW_DEPTH:
            raise export_mod.ExportError(
                f"page views nest more than {export_mod.MAX_VIEW_DEPTH} deep "
                "here; flatten one of the intermediate diagrams")
        budget.spend_render()

        child = ViewResolver(self.renderer, depth=self.depth + 1,
                             seen=self.seen | {(save, page)})
        result = self.renderer.render(rel, spec=spec, rev=rev, resolver=child,
                                      budget=budget)
        return result.data, result.problems


_DEFAULT_RENDERER: Renderer | None = None


def default_resolver() -> ViewResolver:
    """A resolver over the default store and cache, built once, lazily.

    Only reached when nobody registered a factory with
    :func:`awm.drawio.export.set_view_resolver_factory` — the live service does,
    so that a nested render shares the running renderer's cache. The fallback
    exists so that forgetting degrades to slower rather than back to exporting
    a figure that silently points at this host.
    """
    global _DEFAULT_RENDERER
    if _DEFAULT_RENDERER is None:
        from .store import default_root

        _DEFAULT_RENDERER = Renderer(Store(default_root()))
    return ViewResolver(_DEFAULT_RENDERER)


class ViewNotifier:
    """Publishes a ``view-updated`` event on a diagram's emit topic per commit.

    A consumer editor tab that placed one of this diagram's pages as an image
    subscribes to ``drawio:<save>`` and refreshes the image when this arrives.

    Two things make it distinct from :meth:`Service._push_to_live_tabs`, which
    also emits on that topic: it fires on **every** accepted write (not only a
    merge), and **unconditionally** — a consumer can be open when the source has
    no editor tab of its own (an agent ``merge`` is the case that matters most).
    The source diagram's *own* tab also receives it but ignores it: the client
    handles only ``flush``/``push`` for its own save.
    """

    def __init__(self, emit):
        #: ``Callable[[str, Any], Awaitable[None]]`` — the adapter's emit.
        self._emit = emit
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, store: Store,
               loop: asyncio.AbstractEventLoop | None = None) -> None:
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop
        store.subscribe(self.notify)

    def notify(self, save: str, rev: str | None = None) -> None:
        """Schedule the emit on the loop. Cheap and non-blocking: the commit
        hook may run off the loop thread, and nothing writing a diagram should
        wait on a fan-out to consumers."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        topic = f"drawio:{save}"
        payload = {"type": "view-updated", "save": save, "rev": rev}

        def _fire() -> None:
            asyncio.create_task(self._emit(topic, payload))

        try:
            loop.call_soon_threadsafe(_fire)
        except RuntimeError:  # pragma: no cover — loop already closed
            pass


class RenderResult:
    __slots__ = ("data", "etag", "save", "page", "rev", "problems", "cached")

    def __init__(self, data: bytes, etag: str, save: str, page: str | None,
                 rev: str | None, problems: list[str], cached: bool):
        self.data = data
        self.etag = etag
        self.save = save
        self.page = page
        self.rev = rev
        self.problems = problems
        self.cached = cached


# --- the HTTP listener -----------------------------------------------------

def _make_handler(renderer: Renderer):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: D401 — silence stdlib access log
            return

        def _fail(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _rel(self) -> tuple[str, dict]:
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith(VIEW_PREFIX):
                rel = path[len(VIEW_PREFIX):]
            else:  # pragma: no cover — the gateway only routes the prefix here
                rel = path
            # keep_blank_values: `?swap=` must reach the parser to be refused.
            # Dropping it here would be the silent degradation this service is
            # built to refuse — the render would just quietly be the plain one.
            return rel, parse_qs(parts.query, keep_blank_values=True)

        def _meta_headers(self, save: str, page: str | None,
                          rev: str | None) -> None:
            # Same-origin in practice, but expose the headers unconditionally so
            # the consumer client can read them via fetch() to learn the exact
            # topic to subscribe to (free of the save-vs-page path ambiguity).
            self.send_header("X-Drawio-Save", save)
            if page is not None:
                self.send_header("X-Drawio-Page", page)
            if rev:
                self.send_header("X-Drawio-Rev", rev)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Expose-Headers",
                "ETag, X-Drawio-Save, X-Drawio-Page, X-Drawio-Rev, "
                "X-Drawio-Problems")

        def do_HEAD(self) -> None:  # noqa: N802 — stdlib naming
            """Resolve the canonical save/page/rev without rendering — the
            consumer client's unambiguous 'which topic do I subscribe to' probe.

            The spec is validated even though nothing is rendered: a URL whose
            GET can only ever fail should not answer this with a cheerful 200.
            The topic itself does not depend on the parameters, so every variant
            of a page subscribes once and they all refresh together."""
            rel, query = self._rel()
            try:
                renderspec.from_query(query)
            except renderspec.SpecError as exc:
                self._fail(400, str(exc))
                return
            try:
                save, page, rev = renderer.resolve_meta(rel)
            except ViewError as exc:
                self._fail(exc.status, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._fail(502, f"resolve failed: {exc}")
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self._meta_headers(save, page, rev)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            rel, query = self._rel()
            try:
                spec = renderspec.from_query(query)
            except renderspec.SpecError as exc:
                self._fail(400, str(exc))
                return
            # Blank values are kept for the spec's sake, so an empty rev has to
            # be folded back to "no revision" rather than reaching the store.
            rev = (query.get("rev") or [None])[0] or None

            try:
                result = renderer.render(rel, spec=spec, rev=rev)
            except ViewError as exc:
                self._fail(exc.status, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — never 500 a browser image
                log.warning("view render failed for %s: %s", self.path, exc)
                self._fail(502, f"render failed: {exc}")
                return

            inm = self.headers.get("If-None-Match")
            if inm and inm.strip('"') == result.etag:
                self.send_response(304)
                self.send_header("ETag", f'"{result.etag}"')
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(result.data)))
            self.send_header("ETag", f'"{result.etag}"')
            # no-cache = "always revalidate": the browser may keep the bytes but
            # must check ETag first, which is what lets a change-event re-fetch
            # cost a 304 when the page did not actually move.
            self.send_header("Cache-Control", "no-cache")
            self._meta_headers(result.save, result.page, result.rev)
            if result.problems:
                # A placed image degrading to a blank cell is acceptable; doing
                # it silently is not (this store exists because of that class of
                # bug), so the reason rides a header and the log.
                self.send_header("X-Drawio-Problems", str(len(result.problems)))
                log.warning("view of %s has %d unresolved image ref(s): %s",
                            result.save, len(result.problems),
                            "; ".join(result.problems))
            self.end_headers()
            self.wfile.write(result.data)

    return _Handler


class ViewServer:
    """Owns the loopback listener and its ``kind=url`` lease.

    The listener runs in a daemon thread (renders are blocking and already
    serialized on the shared headless browser's lock); the lease is held on the
    service's asyncio loop, mirroring :func:`awm.drawio.mount.hold_mount`.
    """

    def __init__(self, store: Store, renderer: Renderer | None = None):
        self.store = store
        self.renderer = renderer or Renderer(store)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.mounted = False
        self.reason = "not started"

    def start_listener(self) -> int:
        """Bind an ephemeral loopback port and serve in a background thread."""
        handler = _make_handler(self.renderer)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="awm-drawio-view",
            daemon=True)
        self._thread.start()
        log.info("drawio view listener on 127.0.0.1:%d", self.port)
        return self.port

    def status(self) -> dict:
        return {"mounted": self.mounted, "prefix": VIEW_PREFIX,
                "port": self.port, "cache_dir": str(self.renderer.cache_dir),
                "reason": self.reason}

    async def hold_mount(self) -> None:
        """Register the view listener as a ``kind=url`` mount and hold its lease.

        Background work with no caller, so — like the editor mount and the
        autopublish loops — every fault is logged and retried rather than raised.
        """
        hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
        if not hub_url:
            self.reason = "AWM_HUB_URL not set"
            log.error("AWM_HUB_URL not set; drawio view mount cannot register")
            return
        if self.port is None:
            self.start_listener()
        target = f"http://127.0.0.1:{self.port}"

        ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
        ssl_ctx = _ssl_ctx() if ws_base.startswith("wss://") else None

        backoff = 1.0
        while True:
            try:
                async with httpx.AsyncClient(verify=False, timeout=15) as cli:
                    r = await cli.post(f"{hub_url}/hub/register", json={
                        "name": MOUNT_NAME,
                        "prefix": VIEW_PREFIX,
                        "url": target,
                    })
                    r.raise_for_status()
                body = r.json()
                sid, lease_path = body["service_id"], body["lease_ws_path"]
                log.info("drawio view mount up: %s → %s (id=%s)",
                         VIEW_PREFIX, target, sid)
                self.mounted, self.reason = True, "ok"
                backoff = 1.0
                async with websockets.connect(
                    f"{ws_base}{lease_path}",
                    ssl=ssl_ctx, max_size=None, open_timeout=10,
                ) as ws:
                    async for _ in ws:   # first frame is "ready"; then just hold
                        pass
                log.info("drawio view mount lease closed; re-registering")
            except Exception as exc:  # noqa: BLE001 — stay up across any fault
                self.reason = f"{type(exc).__name__}: {exc}"
                log.warning("drawio view mount lost (%s); re-registering in "
                            "%.1fs", exc, backoff)
            finally:
                self.mounted = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # loopback, self-signed gateway cert
    return ctx
