"""A Penpot board's live SVG at a stable URL — on-demand render, cached.

``GET /penpot-view/<file-id>/<page-id>/<board-id>`` (plus ``scale``/``swap``/
``crop`` query parameters, see :mod:`awm.penpot_view.renderspec`) returns the
board's current render as ``image/svg+xml``. This mirrors
:mod:`awm.drawio.view` — the sibling module for drawio pages — closely enough
that the two are worth reading side by side; the *shape* of the listener
(loopback ``ThreadingHTTPServer``, registered as a ``kind=url`` gateway mount
because a Penpot/drawio consumer wants a plain GET, not the
``POST /svc/<name>/fn/<fn>`` shape every other service surface uses) and every
connection-safety hazard below are inherited from it, not rediscovered.

**Freshness: invalidate on change, with the TTL demoted to a rate limit.**
Penpot instruments ``get-file`` with its conditional-loading middleware, so a
``get-file`` carrying ``If-None-Match`` answers **304 in zero bytes** when the
file has not moved, having run only a single-row lookup — and the tag it
compares is built from ``revn``/``vern``/``modified-at``, so it does move on a
real edit. :meth:`ExporterClient.file_etag` wraps that, and this cache takes it
as the ``freshness`` hook.

The two failure modes that shape the design are opposites, and a bare TTL can
only ever trade one for the other: too long and a real edit sits un-reflected;
too short and boards nobody touched re-render on a timer. With a probe,
:data:`DEFAULT_TTL` stops being an expiry and becomes a rate limit on *asking*
— inside it a cached entry is served with no question put to Penpot at all,
past it we spend one cheap probe, and only a genuinely changed source file
costs a render.

Two honest limits. Penpot's tag is per **file**, the only granularity it
exposes, so editing any board in a file marks every cached board of that file
for re-render — coarser than the cache key, which stays per-board. And with no
``freshness`` hook supplied the cache degrades to plain TTL expiry, which is
the materially weaker behaviour; that degradation is a constructor argument,
not a silent default.

Every path that could fail — no hook, a probe that raises, a probe answering
anything but 304/200 — answers *changed*. A freshness check must never be
able to pin a stale render in place.

**The cache key is exactly** :func:`renderspec.cache_key` **— never coarser.**
A render of a composite board must not force a re-render, or a shared cache
slot, with the child boards it embeds; see that function's own docstring.

**Hazards inherited from drawio's listener, verbatim in spirit — the same
shape broke there in production on 2026-08-25:**

* *Drain the request body.* A GET here never carries one, but the gateway's
  URL-proxy has, in the past, invented ``Transfer-Encoding: chunked`` framing
  for every proxied GET regardless of method — and a stdlib
  ``BaseHTTPRequestHandler`` never reads ``rfile`` on its own. The leftover
  chunk terminator then gets parsed as the *next* request line on a reused
  connection, answered with a bare, status-line-less ``400`` (HTTP/0.9,
  because ``parse_request`` fails before it learns the version), and the
  connection drops from underneath whichever pooled request the hub had since
  handed the socket to — an intermittent 500 with no correlated log line on
  the listener side. ``_drain_request_body`` is defence in depth: the real
  fix is the gateway's own ``_forwards_body`` (``awm/gateway/awm/gateway/hub/
  proxy.py``), already in this tree. Do not delete either believing the other
  covers it.
* *Unique temp filenames per in-flight write.* Two renders of one cache key
  sharing a temp filename under a threading server can publish a spliced
  file — and because the file's identity (its ``ETag``) is the render, not a
  hash of its own bytes recomputed on disk, a splice would then answer with a
  perfectly stable ETag *forever*, immune to every invalidation. The cache
  key's own coordination (:class:`Cache`) already ensures at most one render
  is in flight per key at a time, so :meth:`Cache._persist`'s pid+thread-id
  temp name is defence in depth rather than a path this code exercises today
  — kept anyway, because the invariant it protects is exactly the one a
  future refactor is most likely to weaken without noticing.
* *Don't join an in-flight fetch for a stale key.* A background refresh
  arriving while another is already running must not be silently dropped just
  because "a fetch is already in flight" — that fetch may finish with content
  that was already stale again by the time it lands. :class:`Cache` marks the
  entry *dirty* instead and chains a fresh render the moment the running one
  completes, so the later trigger is answered by a fetch that actually started
  after it, not folded into one that started before it.
* ``parse_qs(..., keep_blank_values=True)`` — ``?swap=`` must reach
  :func:`renderspec.from_query` to be refused, not vanish before it can
  complain and quietly render the plain board instead.
* The handler's blocking wait for a cold render is sized to a genuinely slow
  Playwright render plus the exporter's own asset fetch (:data:`
  COLD_RENDER_TIMEOUT`, well beyond ``ExporterClient``'s own 60s+30s budget),
  not stdlib defaults sized for a static file.
* No cache-buster query parameter is defined by this module, on purpose: the
  three parameters it does read (``scale``/``swap``/``crop``) are exactly the
  set a naive future client integration must avoid colliding with when it
  wants to force a refresh. Drawio's own client once busted its cache with
  ``rev=<epoch-ms>`` while drawio's server independently read ``rev`` as a git
  revision selector — so every busted refresh 404ed and the stale image never
  moved. The unknown-parameters-are-ignored contract in
  :func:`renderspec.from_query` means an arbitrary unused key works today, but
  the safest cache-buster is still to change the URL's own identity (a new
  path segment, a different origin query key never read here) rather than
  add a parameter this module might one day read semantically too.
* The handler never lets an unhandled exception reach the stdlib's own
  ``500`` machinery — every failure this module recognises maps to a specific
  status via :class:`ViewError`, and anything else is caught broadly and
  answered ``502`` with a reason. A browser's ``<img>`` tag has no way to show
  *why* it broke; the reason belongs in the response body and the log, not a
  stack trace on stderr.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import websockets

from . import renderspec as R
from . import svgpost as S
from .exporter_client import ExporterError

log = logging.getLogger("awm.penpot_view.view")

#: Re-exported from renderspec so a caller never has to import both modules
#: just to recognise a penpot-view URL.
VIEW_PREFIX = R.VIEW_PREFIX

#: Registered under its own name at the gateway, distinct from any "penpot"
#: service record the rest of this package may hold, mirroring how drawio's
#: view mount (``drawio-view``) is kept apart from the ``drawio`` name.
MOUNT_NAME = os.environ.get("PENPOT_VIEW_MOUNT_NAME", "penpot-view")

#: How long a rendered entry is served without kicking a background refresh.
#: See the module docstring's "Freshness" section: this is a fallback, not
#: the originally-asked-for invalidate-on-change behaviour.
DEFAULT_TTL = float(os.environ.get("PENPOT_VIEW_TTL", "20"))

#: How long a *cold* request (nothing cached yet) will block waiting for the
#: first render. Generous on purpose: ExporterClient's own EXPORT_TIMEOUT
#: (60s, a real headless-browser page load) plus its ASSET_TIMEOUT (30s) are
#: both routine, not a sign of trouble — a tight timeout here would spuriously
#: fail exactly the requests that would otherwise have succeeded.
COLD_RENDER_TIMEOUT = float(os.environ.get("PENPOT_VIEW_COLD_TIMEOUT", "120"))

#: A GET body is always unexpected here; read enough to resynchronise a
#: reused connection and no more.
_MAX_DRAIN = 1 << 20


def default_cache_dir() -> Path:
    """``$AWM_DIR/services/penpot-view/viewcache`` by default.

    Overridable with ``AWM_PENPOT_VIEW_CACHE`` so tests and dev sandboxes
    never share a real deployment's cache. Deliberately dependency-free
    (unlike drawio's ``default_cache_dir``, which reaches into
    ``awm.persistence.databases``): this service's ``pyproject.toml`` does
    not declare ``awm-persistence``, and adding that declaration is outside
    this module's file ownership — see the report for this task.
    """
    override = os.environ.get("AWM_PENPOT_VIEW_CACHE")
    if override:
        return Path(override).expanduser()
    awm_dir = Path(os.environ.get("AWM_DIR", "~/.awm")).expanduser()
    return awm_dir / "services" / "penpot-view" / "viewcache"


class ViewError(Exception):
    """A view request could not be resolved. ``status`` is the HTTP code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --- path parsing ------------------------------------------------------

def parse_path(path: str) -> tuple[str, str, str]:
    """``/penpot-view/<file-id>/<page-id>/<board-id>`` → the three ids.

    Raises :class:`ViewError` (400) for a wrong segment count or a segment
    that is not a canonical Penpot UUID, and (404) for a path outside
    :data:`VIEW_PREFIX` at all — the gateway is only ever expected to route
    the prefix here, so the 404 case is a defensive fallback, not the
    expected shape of a bad request.
    """
    if not path.startswith(VIEW_PREFIX):
        raise ViewError(404, f"{path!r} is not under {VIEW_PREFIX}")
    rest = path[len(VIEW_PREFIX):].strip("/")
    segments = [unquote(s) for s in rest.split("/") if s]
    if len(segments) != 3:
        raise ViewError(
            400,
            f"expected {VIEW_PREFIX}/<file-id>/<page-id>/<board-id>, got "
            f"{path!r}")
    file_id, page_id, board_id = segments
    for label, value in (("file-id", file_id), ("page-id", page_id),
                         ("board-id", board_id)):
        if not R.is_uuid(value):
            raise ViewError(400, f"{label} {value!r} is not a Penpot UUID")
    return file_id, page_id, board_id


# --- the cache -----------------------------------------------------------

class _Fetch:
    """One render in flight for a cache key. Any number of threads may
    :meth:`wait` for it; only the thread running the render calls
    :meth:`finish`/:meth:`fail`.
    """

    __slots__ = ("_done", "data", "etag", "problems", "error")

    def __init__(self) -> None:
        self._done = threading.Event()
        self.data: bytes | None = None
        self.etag: str | None = None
        self.problems: tuple[str, ...] = ()
        self.error: BaseException | None = None

    def finish(self, data: bytes, etag: str, problems: tuple[str, ...]) -> None:
        self.data, self.etag, self.problems = data, etag, problems
        self._done.set()

    def fail(self, error: BaseException) -> None:
        self.error = error
        self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)


class _Entry:
    """One cache slot: the newest rendered bytes plus in-flight coordination.

    ``lock`` guards every field below it; it is only ever held for a quick
    state read/edit, never across an actual render.
    """

    __slots__ = ("lock", "data", "etag", "problems", "rendered_at", "fetch",
                "dirty", "source_etag", "checked_at")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: bytes | None = None
        self.etag: str | None = None
        self.problems: tuple[str, ...] = ()
        self.rendered_at: float = 0.0
        #: The render currently running for this key, if any.
        self.fetch: _Fetch | None = None
        #: Set when a fresh trigger arrives while `fetch` is already running
        #: — see Cache._trigger_refresh and the module docstring's "don't
        #: join a stale fetch" hazard.
        self.dirty = False
        #: Penpot's own ETag for the *source file* at the time these bytes
        #: were rendered, and when we last asked about it. `checked_at`
        #: rate-limits the probe; `source_etag` decides the re-render.
        self.source_etag: str | None = None
        self.checked_at: float = 0.0


class Cache:
    """A stale-while-revalidate cache over one render function, keyed exactly
    on :func:`renderspec.cache_key`.

    A hit within ``ttl`` seconds of its own render is returned as-is. An
    older hit is *still* returned immediately — a warm request never blocks —
    but kicks exactly one background re-render, chained (never dropped) if
    the entry goes stale again before that render lands; see the module
    docstring. A key with nothing cached at all blocks the caller on the
    first render, since there is nothing else to serve.

    Each successful render is also written through to ``cache_dir`` (a
    durability copy — the in-memory entry, not the file, is the source of
    truth for freshness decisions) with a unique per-writer temp name before
    the atomic rename; see the module docstring's temp-filename hazard.
    """

    def __init__(self, cache_dir: Path | None = None, *, ttl: float = DEFAULT_TTL,
                cold_timeout: float = COLD_RENDER_TIMEOUT,
                freshness=None) -> None:
        """``freshness(file_id, known_etag) -> (changed, etag)``, normally
        :meth:`ExporterClient.file_etag`.

        When supplied, ``ttl`` stops being an expiry and becomes a rate limit
        on *asking*: within it a cached entry is served with no question put
        to Penpot at all; past it we spend one cheap probe (a 304 in zero
        bytes when nothing moved), and only a genuinely changed source file
        costs a re-render. That is the difference between "a real edit waits
        out an arbitrary timer" and "an unrelated request re-renders a board
        nobody touched" -- with a probe, neither happens.

        Left ``None``, this degrades to plain TTL expiry, which is materially
        weaker: every entry re-renders on a timer whether or not anything
        changed.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.ttl = ttl
        self.cold_timeout = cold_timeout
        self.freshness = freshness
        self._entries: dict[tuple[str, str, str, str], _Entry] = {}
        self._entries_lock = threading.Lock()
        #: Bumped every time a render actually runs — the test seam for
        #: asserting a cache hit did not re-invoke the exporter.
        self.renders = 0
        self._renders_lock = threading.Lock()

    def _entry_for(self, key: tuple[str, str, str, str]) -> _Entry:
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            return entry

    def _cache_file(self, key: tuple[str, str, str, str]) -> Path:
        file_id, page_id, board_id, fingerprint = key
        return self.cache_dir / file_id / page_id / board_id / f"{fingerprint}.svg"

    def get(self, key: tuple[str, str, str, str], render
            ) -> tuple[bytes, str, tuple[str, ...]]:
        """Return ``(data, etag, problems)`` for ``key``.

        ``render`` is a zero-argument callable performing one real render (an
        exporter call plus post-processing), returning ``(data, problems)``
        and raising on failure. It is invoked at most once per in-flight
        fetch no matter how many callers arrive for the same key concurrently
        — see :meth:`_run_fetch`.
        """
        entry = self._entry_for(key)

        cached = None
        with entry.lock:
            if entry.data is not None:
                cached = (entry.data, entry.etag, entry.problems)
                due = (time.monotonic() - entry.checked_at) >= self.ttl
                known = entry.source_etag
        if cached is not None:
            if due and self._changed(key, entry, known):
                self._trigger_refresh(key, entry, render)
            return cached

        with entry.lock:
            fetch = entry.fetch
            start = fetch is None
            if start:
                fetch = _Fetch()
                entry.fetch = fetch
        if start:
            self._run_fetch(key, entry, fetch, render)
        if not fetch.wait(self.cold_timeout):
            raise TimeoutError(
                f"render for {key!r} did not finish within "
                f"{self.cold_timeout:g}s (a cold render drives a real "
                "headless-browser page load and can routinely take several "
                "seconds)")
        if fetch.error is not None:
            raise fetch.error
        return fetch.data, fetch.etag, fetch.problems

    def _changed(self, key: tuple[str, str, str, str], entry: _Entry,
                 known: str | None) -> bool:
        """Ask Penpot whether the source file moved since we rendered.

        Stamps ``checked_at`` whatever the answer, so a file that is being
        edited continuously does not draw a probe per request.

        With no probe configured this answers "changed", which reproduces
        plain TTL expiry. A probe that itself fails also answers "changed" —
        the failure modes must not be able to pin a stale render in place.
        """
        now = time.monotonic()
        if self.freshness is None:
            with entry.lock:
                entry.checked_at = now
            return True
        try:
            changed, etag = self.freshness(key[0], known)
        except Exception as exc:  # noqa: BLE001 — a probe never blocks a render
            log.warning("penpot-view: freshness probe for %s failed (%s); "
                        "re-rendering rather than trusting the cache", key[0], exc)
            with entry.lock:
                entry.checked_at = now
            return True
        with entry.lock:
            entry.checked_at = now
            if changed and etag is not None:
                # Record it now: the render that follows is what these bytes
                # will correspond to, and a second request arriving mid-render
                # must not queue a duplicate re-render for the same edit.
                entry.source_etag = etag
        return changed

    def _trigger_refresh(self, key: tuple[str, str, str, str], entry: _Entry,
                         render) -> None:
        """Kick a non-blocking background re-render for a stale entry.

        If one is already running for this key, this does not start a
        second — it marks the entry dirty so the running fetch chains a
        fresh render the moment it completes, rather than this trigger being
        silently dropped. See the module docstring.
        """
        with entry.lock:
            if entry.fetch is not None:
                entry.dirty = True
                return
            fetch = _Fetch()
            entry.fetch = fetch
        threading.Thread(
            target=self._run_fetch, args=(key, entry, fetch, render),
            name="penpot-view-refresh", daemon=True).start()

    def _seed_source_etag(self, entry: _Entry, file_id: str) -> None:
        """Record the source file's version *before* rendering it.

        Deliberately before, not after. If an edit lands while a render is
        running and we stamped the post-edit tag onto the pre-edit bytes we
        just produced, the next probe would answer "unchanged" and that entry
        would serve a stale picture forever, with no timer left to rescue it.
        Recording the pre-render tag can at worst cost one redundant
        re-render; recording the post-render tag can lose the edit entirely.
        """
        if self.freshness is None:
            return
        try:
            _changed, etag = self.freshness(file_id, None)
        except Exception as exc:  # noqa: BLE001 — never block a render on this
            log.warning("penpot-view: could not read source etag for %s (%s); "
                        "this entry will re-check on the next probe", file_id, exc)
            return
        with entry.lock:
            entry.source_etag = etag
            entry.checked_at = time.monotonic()

    def _run_fetch(self, key: tuple[str, str, str, str], entry: _Entry,
                   fetch: _Fetch, render) -> None:
        try:
            self._seed_source_etag(entry, key[0])
            data, problems = render()
            with self._renders_lock:
                self.renders += 1
            etag = hashlib.sha256(data).hexdigest()
            self._persist(key, data)
        except BaseException as exc:  # noqa: BLE001 — every joiner must see it
            with entry.lock:
                dirty = entry.dirty
                entry.dirty = False
                entry.fetch = None
                cold = entry.data is None
            fetch.fail(exc)
            if dirty and cold:
                # A key that has never rendered successfully, whose only
                # attempt failed, but which got a fresh trigger meanwhile —
                # worth one more try rather than sitting broken until the
                # next request happens to arrive.
                self._trigger_refresh(key, entry, render)
            else:
                log.warning("penpot-view: render for %s failed: %s", key, exc)
            return

        problems_t = tuple(problems)
        with entry.lock:
            entry.data, entry.etag, entry.problems = data, etag, problems_t
            entry.rendered_at = time.monotonic()
            # A completed render is itself a check: the next probe is due one
            # full TTL from here, not from whenever we last asked. Without
            # this the TTL-only path (no freshness hook) would find every
            # warm entry overdue and re-render on every single request.
            # Only the timestamp — never entry.source_etag, which must keep
            # the pre-render value; see _seed_source_etag.
            entry.checked_at = entry.rendered_at
            dirty = entry.dirty
            entry.dirty = False
            entry.fetch = None
        fetch.finish(data, etag, problems_t)
        if dirty:
            log.info("penpot-view: %s changed again mid-render; re-rendering", key)
            self._trigger_refresh(key, entry, render)

    def _persist(self, key: tuple[str, str, str, str], data: bytes) -> None:
        """Write-through to disk with a unique temp name, then atomic rename.

        At most one render is ever in flight per key (the coordination in
        :meth:`get`/:meth:`_trigger_refresh` guarantees it), so this is
        defence in depth rather than a path exercised today — see the module
        docstring's temp-filename hazard for what breaks if that invariant is
        ever weakened.
        """
        path = self._cache_file(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            # A durability copy, not the source of truth — the in-memory
            # entry already holds the bytes, so a disk failure here degrades
            # a restart's cold-start cost, not correctness.
            log.warning("penpot-view: cache persist for %s failed: %s", key, exc)


# --- rendering -----------------------------------------------------------

class RenderResult:
    """One resolved response: the bytes, their ETag, and any svgpost problems."""

    __slots__ = ("data", "etag", "problems")

    def __init__(self, data: bytes, etag: str, problems: list[str]) -> None:
        self.data = data
        self.etag = etag
        self.problems = problems


class Renderer:
    """Resolves one ``(file, page, board, spec)`` request to cached SVG bytes.

    ``exporter`` needs only an ``export_svg`` method with
    :meth:`awm.penpot_view.exporter_client.ExporterClient.export_svg`'s
    signature — a fake stands in for it in tests exactly as a fake stands in
    for that client's own transport, so neither a live Penpot nor a real
    browser render is needed to exercise the caching and HTTP behaviour here.
    """

    def __init__(self, exporter, *, cache_dir: Path | None = None,
                ttl: float = DEFAULT_TTL, cold_timeout: float = COLD_RENDER_TIMEOUT,
                cache: Cache | None = None) -> None:
        self._exporter = exporter
        self.cache = cache or Cache(cache_dir, ttl=ttl, cold_timeout=cold_timeout)

    @property
    def renders(self) -> int:
        """Bumped each time the exporter is actually invoked — the test seam
        for asserting a cache hit did not re-render."""
        return self.cache.renders

    def _render_once(self, file_id: str, page_id: str, board_id: str,
                     spec: R.RenderSpec):
        def render() -> tuple[bytes, list[str]]:
            try:
                svg = self._exporter.export_svg(
                    file_id=file_id, page_id=page_id, object_id=board_id,
                    name="board", scale=spec.scale)
            except ExporterError as exc:
                raise ViewError(502, f"export failed: {exc}") from exc
            try:
                return S.postprocess(svg, spec)
            except S.ShapeNotFound as exc:
                raise ViewError(404, str(exc)) from exc
            except S.SvgPostError as exc:
                raise ViewError(422, str(exc)) from exc
        return render

    def render(self, file_id: str, page_id: str, board_id: str,
              spec: R.RenderSpec) -> RenderResult:
        """Validate, key, and serve.

        Renders fresh only on a cold miss or a TTL-triggered background
        refresh (see :class:`Cache`). Raises :class:`ViewError` for every
        caller-facing failure this module recognises (a bad id, an unknown
        crop shape, an export failure); the handler's broad ``except`` is a
        last resort for anything this misses, never the intended path.
        """
        for label, value in (("file-id", file_id), ("page-id", page_id),
                             ("board-id", board_id)):
            if not R.is_uuid(value):
                raise ViewError(400, f"{label} {value!r} is not a Penpot UUID")
        key = R.cache_key(file_id, page_id, board_id, spec)
        data, etag, problems = self.cache.get(
            key, self._render_once(file_id, page_id, board_id, spec))
        return RenderResult(data, etag, list(problems))


# --- the HTTP listener -----------------------------------------------------

def _make_handler(renderer: Renderer):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # A cold render can run a real headless-browser page load for several
        # seconds; the socket must not look hung mid-render. The wait that
        # actually governs a cold request is Cache.cold_timeout, not this.
        timeout = COLD_RENDER_TIMEOUT

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

            See the module docstring's drain hazard: this is defence in
            depth against the gateway inventing chunked framing for a
            bodyless GET, not the fix itself. A declared ``Content-Length``
            is consumed; any other framing closes the connection instead of
            hand-rolling a chunked decoder.
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

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            self._drain_request_body()
            parts = urlsplit(self.path)
            try:
                file_id, page_id, board_id = parse_path(parts.path)
            except ViewError as exc:
                self._fail(exc.status, str(exc))
                return

            # keep_blank_values: `?swap=` must reach the parser so it can be
            # refused, rather than vanishing on the way and quietly rendering
            # the plain board.
            query = parse_qs(parts.query, keep_blank_values=True)
            try:
                spec = R.from_query(query)
            except R.SpecError as exc:
                self._fail(400, str(exc))
                return

            try:
                result = renderer.render(file_id, page_id, board_id, spec)
            except ViewError as exc:
                self._fail(exc.status, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — never 500 a browser image
                log.warning("penpot-view: render failed for %s: %s",
                           self.path, exc)
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
            # no-cache = "always revalidate": a client may keep the bytes but
            # must check the ETag first, so a refresh costs a 304 when
            # nothing actually moved.
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Expose-Headers", "ETag, X-Penpot-Problems")
            if result.problems:
                # A degraded picture is acceptable; a silently degraded one
                # is not — the reason rides a header and the log both.
                self.send_header("X-Penpot-Problems", str(len(result.problems)))
                log.warning(
                    "penpot-view: render of %s/%s/%s has %d problem(s): %s",
                    file_id, page_id, board_id, len(result.problems),
                    "; ".join(result.problems))
            self.end_headers()
            self.wfile.write(result.data)

    return _Handler


class ViewServer:
    """Owns the loopback listener and its ``kind=url`` gateway lease.

    Mirrors :class:`awm.drawio.view.ViewServer`, the worked precedent for
    this shape. The listener runs in a daemon thread; the lease is held on
    the service's asyncio loop.
    """

    def __init__(self, renderer: Renderer) -> None:
        self.renderer = renderer
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
            target=self._httpd.serve_forever, name="awm-penpot-view",
            daemon=True)
        self._thread.start()
        log.info("penpot-view listener on 127.0.0.1:%d", self.port)
        return self.port

    def status(self) -> dict:
        return {"mounted": self.mounted, "prefix": VIEW_PREFIX,
                "port": self.port, "cache_dir": str(self.renderer.cache.cache_dir),
                "reason": self.reason}

    async def hold_mount(self) -> None:
        """Register the view listener as a ``kind=url`` mount and hold its
        lease. Background work with no caller, so every fault is logged and
        retried rather than raised, mirroring drawio's own mount loop."""
        hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
        if not hub_url:
            self.reason = "AWM_HUB_URL not set"
            log.error("AWM_HUB_URL not set; penpot-view mount cannot register")
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
                log.info("penpot-view mount up: %s -> %s (id=%s)",
                         VIEW_PREFIX, target, sid)
                self.mounted, self.reason = True, "ok"
                backoff = 1.0
                async with websockets.connect(
                    f"{ws_base}{lease_path}",
                    ssl=ssl_ctx, max_size=None, open_timeout=10,
                ) as ws:
                    async for _ in ws:   # first frame is "ready"; then just hold
                        pass
                log.info("penpot-view mount lease closed; re-registering")
            except Exception as exc:  # noqa: BLE001 — stay up across any fault
                self.reason = f"{type(exc).__name__}: {exc}"
                log.warning("penpot-view mount lost (%s); re-registering in "
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
