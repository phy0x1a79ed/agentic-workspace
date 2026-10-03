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

Reaching that key is the expensive part — a parse, a colour pass, every
referenced file read, a render of every page this one places — so a cheap
precheck sits in front of it, deciding from ``stat`` alone whether any of that
could have changed. It is a fast path, not a second source of truth: the
``ETag`` is still the content key, and a precheck miss simply costs the full
pass.

**The pipeline is scoped to the page that was asked for.** The document is cut
to that one page before anything else happens, so a request resolves the
references that page places and no others. Without the cut, asking for any page
of a diagram whose *other* pages hold live views resolved all of them —
recursively, exhausting the render budget — and then discarded the result.

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
import time
from collections import OrderedDict
from contextlib import contextmanager
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

#: How many warm-path answers to remember in process. Each entry is two hashes
#: and a few strings, so the bound is about not holding stale keys forever
#: rather than about memory.
MAX_WARM_ENTRIES = 256


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


@contextmanager
def _span(marks: list[str], name: str):
    """Time a stage of a render and record it for the request's debug line.

    Cheap enough to leave unconditional: the whole point is that the cost of a
    view request is readable from the service log without a profiler.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        marks.append(f"{name}={(time.perf_counter() - start) * 1000:.0f}ms")


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
        #: Precheck key -> (content key, problems), newest last. See the warm
        #: path below.
        self._warm: OrderedDict[str, tuple[str, tuple[str, ...]]] = OrderedDict()
        #: save -> (stat signature, head revision).
        self._revs: dict[str, tuple[tuple[int, int] | None, str | None]] = {}
        #: save -> (stat signature, (/files paths, other diagrams)).
        self._refs: dict[str, tuple[tuple[int, int] | None, tuple]] = {}
        #: Guards the LRU only. ``_revs`` and ``_refs`` are plain dicts whose
        #: entries are written whole, so a race there costs a recomputation.
        self._memo_lock = threading.Lock()

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

    def _cut_to_page(self, xml: str, name: str | None) -> tuple[str, int | None]:
        """Resolve a page name and reduce the document to that page alone.

        Resolution reuses the same name→index contract autopublish uses, so a
        reordered tab never silently repoints a view. The cut is what keeps a
        request for one page from doing the rest of the document's work: on a
        diagram whose *other* pages place live views, inlining the whole thing
        resolved every one of them and then threw the result away.

        A page-omitted request means "the whole document, let the exporter
        decide", so it is left whole — and, as today, never parsed, which is why
        a compressed diagram still renders plainly.
        """
        from .autopublish import AutoPublishError, page_index

        if name is None:
            return xml, None
        mxfile = xmlmodel.parse(xml)
        try:
            index = page_index(mxfile, name)
        except AutoPublishError as exc:
            raise ViewError(404, str(exc)) from None
        return xmlmodel.single_page(mxfile, index), index

    def _content_key(self, inlined: str, index: int | None,
                     spec: renderspec.RenderSpec, *, at: int | None = None) -> str:
        """Hash the *specific page's* inlined content (whole doc if page-omitted).

        Keying per page — not per whole document — is what keeps an unchanged
        page's cache valid across a commit that only touched a sibling page.

        ``index`` is the page's position in the author's document and is part of
        the key; ``at`` is where that page sits inside ``inlined``, which is 0
        once the document has been cut down to it. Keeping the two apart is what
        makes a cut render key-identical to the whole-document render it
        replaced, so no already-cached page is orphaned.

        The spec's fingerprint joins the hash only when it is not the plain one,
        which keeps every existing cache entry valid across this change. It is
        belt-and-braces given the swapped material already differs, but it is
        what guarantees two variants can never collide on an ETag — and the
        ETag is what the revalidation path turns on.
        """
        material = inlined
        where = index if at is None else at
        if where is not None:
            try:
                mxfile = xmlmodel.parse(inlined)
                diagrams = mxfile.findall("diagram")
                if 0 <= where < len(diagrams):
                    material = xmlmodel.serialize(diagrams[where])
            except xmlmodel.MalformedDiagram:
                material = inlined  # hash the whole thing rather than crash
        digest = hashlib.sha256()
        digest.update(f"v1|{index}|{spec.scale:.4f}|".encode("utf-8"))
        if not spec.is_plain:
            digest.update(f"{renderspec.fingerprint(spec)}|".encode("utf-8"))
        digest.update(material.encode("utf-8"))
        return digest.hexdigest()

    # -- the warm path ------------------------------------------------------
    #
    # The content key is a hash of the *inlined* document, which is what makes
    # it trustworthy and also what makes it expensive: reaching it costs a
    # parse, a colour pass, every referenced file read, and a render of every
    # page this one places. The precheck below reaches the same answer from
    # ``stat`` alone. It is strictly a fast path in front of the content key —
    # nothing about what that key means changes, and a miss costs exactly what
    # a request cost before this existed.

    @staticmethod
    def _stat_sig(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def _head_rev(self, save: str) -> str | None:
        """:meth:`Store.head_rev`, memoized on the diagram's stat.

        It forks ``git log`` to fill one response header, and a page that places
        a dozen sibling views pays that fork a dozen times over. A commit can
        move the revision without moving the file, which is why the commit hook
        drops this rather than the stat alone being trusted to expire it.
        """
        sig = self._stat_sig(self.store.abs_path(save))
        remembered = self._revs.get(save)
        if remembered is not None and remembered[0] == sig:
            return remembered[1]
        rev = self.store.head_rev(save)
        self._revs[save] = (sig, rev)
        return rev

    def _references(self, save: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        """``(/files paths, other diagrams)`` this document points at, memoized
        on its stat.

        Scanning half a megabyte of XML costs an order of magnitude more than
        every ``stat`` the precheck then does with the answer, and the answer
        only changes when the file does. A scan that could not resolve one of
        its references is not remembered, so a diagram created later is picked
        up rather than being cached as broken.
        """
        sig = self._stat_sig(self.store.abs_path(save))
        if sig is None:
            return None
        remembered = self._refs.get(save)
        if remembered is not None and remembered[0] == sig:
            return remembered[1]
        try:
            xml = self.store.read(save)
        except (StoreError, OSError):
            return None
        files: list[str] = []
        others: list[str] = []
        for match in export_mod.REFERENCE_PATTERN.finditer(xml):
            if match.group("file") is not None:
                if match.group("file") not in files:
                    files.append(match.group("file"))
                continue
            try:
                rel, _ = split_view_url(
                    export_mod.unescape_amp(match.group("view")))
                other, _ = resolve_target(self.store, rel)
            except (ViewError, StoreError):
                return None
            if other != save and other not in others:
                others.append(other)
        found = (tuple(files), tuple(others))
        self._refs[save] = (sig, found)
        return found

    def _precheck_key(self, save: str, page: str | None,
                      spec: renderspec.RenderSpec,
                      seen: frozenset[str] = frozenset()) -> str | None:
        """Hash everything a ``stat`` can see that this render depends on.

        That is: the spec, and the ``(mtime_ns, size)`` of the diagram and of
        every file the *document* references. Deliberately the whole document
        rather than the target page — that is what a scan can answer without
        parsing, and over-approximating only ever costs a slow path that would
        have been correct anyway.

        A reference to a page of this same diagram adds nothing: its content is
        already in this file's stat. A reference into another diagram folds in
        that diagram's key, recursively, behind the same cycle guard
        :class:`ViewResolver` carries.

        ``None`` means "cannot answer" — a missing file, a reference that will
        not resolve — and the caller must take the slow path. A file rewritten
        within one mtime tick *and* to the same size would slip through; that is
        what the size component is there to make unlikely.
        """
        sig = self._stat_sig(self.store.abs_path(save))
        found = self._references(save)
        if sig is None or found is None:
            return None
        files, others = found
        parts = [f"v1|{save}|{page}|{spec.scale:.4f}|"
                 f"{renderspec.fingerprint(spec)}|{sig[0]}:{sig[1]}"]
        for target in files:
            parts.append(f"f|{target}|{self._stat_sig(Path(target))}")
        seen = seen | {save}
        for other in others:
            if other in seen:
                continue
            seen = seen | {other}
            nested = self._precheck_key(other, None, renderspec.DEFAULT, seen)
            if nested is None:
                return None
            parts.append(f"v|{other}|{nested}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _warm_hit(self, warm_key: str | None, save: str, page: str | None,
                  spec: renderspec.RenderSpec,
                  head_rev: str | None) -> "RenderResult | None":
        """The render this precheck key named, if it is still on disk."""
        with self._memo_lock:
            entry = self._warm.get(warm_key) if warm_key else None
        if entry is None:
            return None
        key, problems = entry
        try:
            data = (self._variant_dir(save, page, spec) / f"{key}.svg").read_bytes()
        except OSError:
            with self._memo_lock:         # the render it named was reclaimed
                self._warm.pop(warm_key, None)
            return None
        with self._memo_lock:
            if warm_key in self._warm:
                self._warm.move_to_end(warm_key)
        return RenderResult(data, key, save, page, head_rev, list(problems),
                            cached=True)

    def _remember(self, warm_key: str | None, key: str,
                  problems: list[str]) -> None:
        if warm_key is None:
            return
        with self._memo_lock:
            self._warm[warm_key] = (key, tuple(problems))
            self._warm.move_to_end(warm_key)
            while len(self._warm) > MAX_WARM_ENTRIES:
                self._warm.popitem(last=False)

    def resolve_meta(self, rel_path: str) -> tuple[str, str | None, str | None]:
        """Resolve ``(save, page, head_rev)`` without rendering — the cheap HEAD
        answer the consumer client uses to learn which topic to subscribe to,
        free of the save-vs-page path ambiguity a client cannot settle alone."""
        save, page_name = self.resolve_target(rel_path)
        return save, page_name, self._head_rev(save)

    def render(self, rel_path: str, spec: renderspec.RenderSpec | None = None,
               rev: str | None = None, resolver=None,
               budget=None) -> "RenderResult":
        """Resolve, render (or hit the cache), and return the SVG + metadata.

        Never returns a half-document: a missing diagram or unknown page raises
        :class:`ViewError` with a 404, which the handler turns into an honest
        error rather than a blank image. An unknown crop frame is a 404 for the
        same reason — a parameter that silently does nothing is worse than one
        that fails.

        The order is: read, cut to the requested page, transform for the spec,
        inline, then key over that final material. Cutting first is what bounds
        the work to the page asked for — every reference resolved from here down
        is one that page actually places. Swapping before inlining means the
        colour rewrite only ever sees ``/files`` references, never a
        percent-encoded payload it might corrupt.

        A consequence worth knowing: problems belonging to *other* pages no
        longer surface here. ``Service.check`` is the surface that audits a whole
        document.
        """
        spec = spec or renderspec.DEFAULT
        save, page_name = resolve_target(self.store, rel_path)
        timings: list[str] = []
        try:
            with _span(timings, "rev"):
                head_rev = rev or self._head_rev(save)

            # Before the read, not after: a warm request should not pull half a
            # megabyte off disk to discover it had the answer already. A request
            # pinned to a revision is not what the working tree's stat
            # describes, so it never takes this path at all.
            warm_key = None
            if rev is None:
                with _span(timings, "precheck"):
                    warm_key = self._precheck_key(save, page_name, spec)
                    warm = self._warm_hit(warm_key, save, page_name, spec,
                                          head_rev)
                if warm is not None:
                    return warm

            try:
                xml = self.store.read(save, rev=rev)
            except UnknownDiagram as exc:
                raise ViewError(404, str(exc)) from None
            except StoreError as exc:
                raise ViewError(404, str(exc)) from None

            with _span(timings, "cut"):
                xml, index = self._cut_to_page(xml, page_name)
            # Where the target page sits in `xml` from here on: the cut put it
            # first, and a page-omitted request never cut, so it stays whole.
            at = None if index is None else 0

            swap_problems: list[str] = []
            if spec.swaps:
                try:
                    xml, hits, swap_problems = renderspec.swap_document(
                        xml, spec.swaps)
                except xmlmodel.CompressedDiagram as exc:
                    raise ViewError(
                        422, f"this diagram is compressed, so its colours cannot "
                             f"be swapped: {exc}") from None
                except xmlmodel.MalformedDiagram as exc:
                    raise ViewError(422, str(exc)) from None
                if hits == 0:
                    # Not an error — a mask may live on one page and not another
                    # — but not silent either, since "nothing happened" and "the
                    # parameter was ignored" look identical from the outside.
                    log.warning("view of %s%s: no colour matched %s", save,
                                f" page {page_name!r}" if page_name else "",
                                renderspec.describe(spec))

            crop_id = None
            if spec.crop:
                try:
                    xml, crop_id = renderspec.prepare_crop(xml, at, spec.crop)
                except renderspec.CropNotFound as exc:
                    raise ViewError(404, str(exc)) from None
                except xmlmodel.CompressedDiagram as exc:
                    raise ViewError(422, str(exc)) from None

            # Inline once here (for the content key) and hand the already-inlined
            # document to render with inline=False, so the /files bytes are read
            # a single time per request rather than twice. This also strictly
            # precedes the render call, which is what keeps a nested page view
            # from re-entering the browser's non-reentrant lock.
            if resolver is None:
                resolver = ViewResolver(self, seen=frozenset({(save, page_name)}))
            with _span(timings, "inline"):
                inlined, problems = export_mod.inline_images(
                    xml, swaps=spec.swaps, resolver=resolver, budget=budget)
            problems = swap_problems + problems
            with _span(timings, "key"):
                key = self._content_key(inlined, index, spec, at=at)

            self._remember(warm_key, key, problems)
            variant_dir = self._variant_dir(save, page_name, spec)
            cache_file = variant_dir / f"{key}.svg"
            if cache_file.is_file():
                return RenderResult(cache_file.read_bytes(), key, save, page_name,
                                    head_rev, problems, cached=True)

            with _span(timings, "browser"):
                data, _ = self._render(inlined, "svg", inline=False, page=at,
                                       scale=spec.scale, crop_id=crop_id)
            self.renders += 1
            variant_dir.mkdir(parents=True, exist_ok=True)
            # The listener is a ThreadingHTTPServer and the export subprocess
            # is slow, so two requests for the same page and variant overlap
            # routinely. A shared temp name lets them interleave into one file
            # and publish the splice — which, being content-addressed, then
            # answers with a stable ETag forever. Name it per writer instead.
            tmp = cache_file.with_name(
                f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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
        finally:
            if timings:
                log.debug("view %s%s%s: %s", save,
                          f"/{page_name}" if page_name else "",
                          "" if spec.is_plain else f"?{renderspec.to_query(spec)}",
                          " ".join(timings))

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

        The in-process memos go first and go whole. A commit moves the revision
        without necessarily moving the file — the write landed before it — so
        the stat those memos key on cannot be trusted to have expired them.
        """
        with self._memo_lock:
            self._warm.clear()
        self._revs.clear()
        self._refs.clear()
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
    subscribes to ``drawio:<save>:<page>`` (percent-encoded page name) and
    refreshes the image when this arrives. A page-omitted (whole-document)
    reference subscribes to the unscoped ``drawio:<save>`` instead, which is
    always emitted alongside the page-scoped topics regardless of which pages
    changed — that reference's granularity is the whole document by design.

    Two things make it distinct from :meth:`Service._push_to_live_tabs`, which
    also emits on the unscoped topic: it fires on **every** accepted write (not
    only a merge), and **unconditionally** — a consumer can be open when the
    source has no editor tab of its own (an agent ``merge`` is the case that
    matters most). The source diagram's *own* tab also receives it but ignores
    it: the client handles only ``flush``/``push`` for its own save.

    Page-scoped topics are derived by diffing the just-committed content
    against the immediately prior revision of *this path* (``store.history``
    is already ``git log -- path``-scoped, so it is robust to unrelated
    commits landing in between). Anything that stops that diff from being
    trustworthy — no prior revision, a read/parse failure — falls back to
    treating every current page as changed: over-notifying is a wasted
    refresh, under-notifying is a consumer stuck showing stale content.
    """

    def __init__(self, emit):
        #: ``Callable[[str, Any], Awaitable[None]]`` — the adapter's emit.
        self._emit = emit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._store: Store | None = None

    def attach(self, store: Store,
               loop: asyncio.AbstractEventLoop | None = None) -> None:
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop
        self._store = store
        store.subscribe(self.notify)

    def _changed_pages(self, save: str, rev: str | None) -> list[str] | None:
        """Names of pages that changed in ``rev``, or ``None`` for "all of them".

        ``None`` is the defensive fallback — every path here that cannot
        establish a trustworthy diff (no prior revision, unreadable/unparsable
        content on either side) returns it rather than guessing.
        """
        store = self._store
        if store is None:
            return None
        try:
            history = store.history(save, limit=2)
            if len(history) < 2:
                return None  # first commit for this path — nothing to diff
            prior_rev = history[1].rev
            new_xml = store.read(save, rev=rev) if rev else store.read(save)
            old_xml = store.read(save, rev=prior_rev)
            new_pages = xmlmodel.parse(new_xml).findall("diagram")
            old_pages = xmlmodel.parse(old_xml).findall("diagram")
        except Exception:  # noqa: BLE001 — any failure means "diff untrustworthy"
            return None

        def key(diagram) -> str | None:
            return diagram.get("id") or diagram.get("name")

        old_by_key = {key(d): d for d in old_pages if key(d) is not None}
        changed = []
        for diagram in new_pages:
            name = diagram.get("name")
            if not name:
                continue
            old = old_by_key.get(key(diagram))
            if old is None or xmlmodel.serialize(old) != xmlmodel.serialize(diagram):
                changed.append(name)
        return changed

    def notify(self, save: str, rev: str | None = None) -> None:
        """Schedule the emit(s) on the loop. Cheap and non-blocking: the commit
        hook may run off the loop thread, and nothing writing a diagram should
        wait on a fan-out to consumers."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return

        topics = [f"drawio:{save}"]
        changed_pages = self._changed_pages(save, rev)
        if changed_pages is None:
            changed_pages = []
            store = self._store
            if store is not None:
                try:
                    xml = store.read(save, rev=rev) if rev else store.read(save)
                    pages = xmlmodel.page_summaries(xmlmodel.parse(xml))
                    changed_pages = [p["name"] for p in pages if p.get("name")]
                except Exception:  # noqa: BLE001 — unknown pages is not fatal
                    changed_pages = []
        for page in changed_pages:
            topics.append(f"drawio:{save}:{quote(page, safe='')}")

        payload = {"type": "view-updated", "save": save, "rev": rev}

        def _fire() -> None:
            for topic in topics:
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

# A GET/HEAD body is always a peer bug here; read enough to resynchronise the
# connection and no more, rather than letting one become a memory sink.
_MAX_DRAIN = 1 << 20


def _make_handler(renderer: Renderer, renderer_for=None):
    """``renderer_for(as_)`` picks a renderer for the request's ``X-Awm-As``
    (a per-user store); ``None`` from it, or no resolver, means ``renderer``."""
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @property
        def renderer(self) -> Renderer:
            if renderer_for is not None:
                chosen = renderer_for(self.headers.get("X-Awm-As"))
                if chosen is not None:
                    return chosen
            return renderer

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

        def _drain_request_body(self) -> None:
            """Leave no unread request bytes on a connection we will reuse.

            Defence in depth, not the fix: the gateway's URL proxy used to
            invent ``Transfer-Encoding: chunked`` on every bodyless proxied GET
            (see ``awm.gateway.hub.proxy._forwards_body``), and this handler —
            like every ``BaseHTTPRequestHandler`` — never reads ``rfile``. The
            leftover chunk terminator was then parsed as the next request line,
            answered ``400``, and the connection dropped underneath whoever the
            proxy's pool had handed it to next. Do not delete the gateway fix
            believing this covers it: this only stops a *malformed* peer from
            desynchronising us, and it costs the connection when it does.

            A declared ``Content-Length`` is consumed. Any other framing closes
            the connection instead — a diagram service has no business
            hand-rolling a chunked decoder, and closing is correct absolutely
            where a decoder is correct only if it is right.
            """
            if "transfer-encoding" in self.headers:
                self.close_connection = True
                return
            raw = self.headers.get("content-length")
            if not raw:
                return
            try:
                n = int(raw)
            except ValueError:
                self.close_connection = True
                return
            if n <= 0:
                return
            if n > _MAX_DRAIN:
                self.close_connection = True
                return
            try:
                self.rfile.read(n)
            except OSError:
                self.close_connection = True

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
            self._drain_request_body()
            rel, query = self._rel()
            try:
                renderspec.from_query(query)
            except renderspec.SpecError as exc:
                self._fail(400, str(exc))
                return
            try:
                save, page, rev = self.renderer.resolve_meta(rel)
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
            self._drain_request_body()
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
                result = self.renderer.render(rel, spec=spec, rev=rev)
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

    def __init__(self, store: Store, renderer: Renderer | None = None,
                 renderer_for=None):
        self.store = store
        self.renderer = renderer or Renderer(store)
        self.renderer_for = renderer_for
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.mounted = False
        self.reason = "not started"

    def start_listener(self) -> int:
        """Bind an ephemeral loopback port and serve in a background thread."""
        handler = _make_handler(self.renderer, self.renderer_for)
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
