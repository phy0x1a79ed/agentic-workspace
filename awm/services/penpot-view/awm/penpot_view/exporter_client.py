"""A persistent service-account session against Penpot: export, then fetch.

Producing an SVG for a board is two HTTP hops against two different Penpot
components, and getting either hop wrong produces a *blank or stale* SVG
rather than a visible error -- the exact failure class this module exists to
refuse.

**Hop 1 -- export.** ``POST export-shapes`` to the exporter (port 6061 inside
the compose network), which drives a real headless-browser render and, with
``wait: true``, blocks until it is done and the result is uploaded. It answers
with a resource map naming where the rendered asset now lives: ``{:uri
"<PENPOT_PUBLIC_URI>/assets/by-id/<id>"}``.

**Hop 2 -- fetch, and only through the frontend.** That URI must be fetched
through Penpot's frontend nginx, never by talking to the backend/storage layer
directly. The local stack stores objects on the filesystem
(``PENPOT_OBJECTS_STORAGE_BACKEND=fs``), and the backend's own object handler
answers a bare asset request with **HTTP 204, an ``x-accel-redirect`` header,
and zero bytes** -- it delegates turning that into actual bytes to an
``internal`` nginx location the backend itself does not have. A client that
"helpfully" shortcuts past the frontend gets a clean-looking 204 and produces
an empty SVG that reports as a successful export. So every asset fetch here
first checks the returned URI actually starts with the frontend base URL, and
then treats *any* non-200, non-``image/svg+xml``, or zero-byte response as a
hard failure -- never as "unchanged" or "try again quietly". The asset fetch
also needs the same ``auth-token`` cookie as the export call: ``tempfile`` is
not in Penpot's public-buckets list, so an unauthenticated GET comes back as
an attachment download, not the raw SVG.

Both hops speak ``application/transit+json``, which has no Python library.
The ``_transit_dumps``/``_transit_loads`` pair below hand-roll transit's
``json-verbose`` encoding for exactly the subset Penpot's RPC layer uses --
keywords, uuids, strings, numbers, booleans, nil, maps and vectors -- and
nothing more.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from .renderspec import is_uuid

log = logging.getLogger("awm.penpot_view.exporter_client")

#: The frontend nginx -- the only thing an asset fetch may ever go through.
DEFAULT_BASE_URL = os.environ.get("PENPOT_BASE_URL", "http://localhost:9001")
#: The exporter, reached directly inside the compose network for the export
#: POST itself (only the *asset fetch* that follows is nginx-only).
#: The exporter listens on 6061 *inside* the compose network and publishes no
#: host port, so 6061 is unreachable from anywhere but a sibling container.
#: Penpot's own frontend nginx proxies ``/api/export`` to it (``proxy_pass
#: http://penpot-exporter:6061`` with no URI part, so the full path is
#: forwarded and the exporter's dispatcher — which routes on everything except
#: ``/readyz`` — accepts it). Going through the frontend therefore works from
#: the host *and* from inside the network, and it is the same origin the asset
#: fetch must use anyway. Verified live: this route answers with the
#: exporter's own spec-validation error, so it is genuinely the exporter.
DEFAULT_EXPORTER_URL = os.environ.get("PENPOT_EXPORTER_URL", "http://localhost:9001")
DEFAULT_USERNAME = os.environ.get("PENPOT_SERVICE_USERNAME")
DEFAULT_PASSWORD = os.environ.get("PENPOT_SERVICE_PASSWORD")

#: Every backend RPC hangs off this prefix; the command is the last segment.
RPC_PREFIX = "/api/rpc/command"
LOGIN_PATH = f"{RPC_PREFIX}/login-with-password"
#: Paired with DEFAULT_EXPORTER_URL above: this is the location Penpot's
#: frontend nginx proxies to the exporter. The exporter itself dispatches on
#: the transit body's ``cmd`` and routes on every path except ``/readyz``
#: (``exporter/src/app/http.cljs``), so the segment matters only to nginx, not
#: to the exporter. Override both together if the deployment differs.
EXPORT_PATH = os.environ.get("PENPOT_EXPORTER_EXPORT_PATH", "/api/export")

#: Explicit everywhere, on purpose -- see the shared hub client in
#: awm/gateway/awm/gateway/hub/proxy.py, whose timeout=None means a wedged
#: exporter would otherwise pin a hub request forever.
LOGIN_TIMEOUT = float(os.environ.get("PENPOT_LOGIN_TIMEOUT", "15"))
#: A cold render drives a real headless-browser page load with networkidle
#: and font-settle waits -- several seconds is routine, not a sign of trouble.
EXPORT_TIMEOUT = float(os.environ.get("PENPOT_EXPORT_TIMEOUT", "60"))
ASSET_TIMEOUT = float(os.environ.get("PENPOT_ASSET_TIMEOUT", "30"))
#: The freshness probe is a single-row lookup on the 304 path, so it gets a
#: much shorter leash than a render -- if it is slow, re-rendering is cheaper
#: than waiting for it.
FRESHNESS_TIMEOUT = float(os.environ.get("PENPOT_FRESHNESS_TIMEOUT", "10"))
#: Sub-resources are small (a font, one image fill) next to a render, so they
#: get a short leash of their own rather than the asset fetch's.
SUBRESOURCE_TIMEOUT = float(os.environ.get("PENPOT_SUBRESOURCE_TIMEOUT", "20"))
#: Refuse to pull an unbounded blob into an inlined SVG. A board carrying a
#: fill larger than this keeps its external reference and reports a problem,
#: which is visible, rather than producing a render nobody can load.
MAX_SUBRESOURCE_BYTES = int(
    os.environ.get("PENPOT_MAX_SUBRESOURCE_BYTES", str(8 * 1024 * 1024)))

COOKIE_NAME = "auth-token"


class ExporterError(RuntimeError):
    """A Penpot login, export, or asset fetch failed. Always names what."""


# --- transit (json-verbose subset) ------------------------------------------
#
# Kept private and minimal: keywords, uuids, strings, numbers, booleans, nil,
# maps and vectors -- exactly what export-shapes' request and response use.
# Nothing here attempts the rest of the transit spec (sets, dates, symbols,
# extension types); an unrecognised `~`-prefixed string decodes to itself
# rather than raising, since a Penpot response can carry fields (timestamps,
# etc.) this module has no use for and must not choke on.


class Keyword(str):
    """A transit keyword -- encodes as ``~:name``, unlike a plain string."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return f"Keyword({str.__str__(self)!r})"


def _transit_key(key: Any) -> str:
    if isinstance(key, Keyword):
        return f"~:{key}"
    if isinstance(key, uuid.UUID):
        return f"~u{key}"
    if isinstance(key, str):
        return f"~{key}" if key.startswith("~") else key
    raise TypeError(f"transit encode: unsupported map key type {type(key)!r}")


def _encode_transit(value: Any) -> Any:
    if isinstance(value, Keyword):
        return f"~:{value}"
    if isinstance(value, uuid.UUID):
        return f"~u{value}"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return f"~{value}" if value.startswith("~") else value
    if isinstance(value, Mapping):
        return {_transit_key(k): _encode_transit(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_transit(v) for v in value]
    raise TypeError(f"transit encode: unsupported type {type(value)!r}")


#: Transit's read cache, verbatim from the spec's reference implementations:
#: codes are ``^`` plus one or two base-44 digits starting at ASCII ``0``.
_CACHE_BASE = 44
_CACHE_FIRST = 48
#: Only strings of four characters or more are ever cached.
_CACHE_MIN = 4
#: The marker that makes an array a map: ``["^ ", k, v, k, v, …]``.
_MAP_AS_ARRAY = "^ "
#: Prefix of a transit *tag*. A tagged value arrives either as
#: ``{"~#tag": rep}`` (verbose) or ``["~#tag", rep]`` (compact); both are
#: unwrapped to the representation, which is what a client reading a handful
#: of fields wants. The tag itself is discarded on purpose -- this module
#: needs the URI string behind ``~#uri``, not a typed URI object.
_TAG = "~#"


def _code_to_index(code: str) -> int:
    if len(code) == 2:
        return ord(code[1]) - _CACHE_FIRST
    return ((ord(code[1]) - _CACHE_FIRST) * _CACHE_BASE
            + (ord(code[2]) - _CACHE_FIRST))


def _is_cacheable(text: str, as_map_key: bool) -> bool:
    """Whether the writer would have cached this, so our indices stay aligned.

    Getting this predicate wrong is worse than not caching at all: the cache
    is positional, so one wrongly-admitted string shifts every later index and
    silently decodes the rest of the document into the wrong values.
    """
    if len(text) < _CACHE_MIN:
        return False
    return as_map_key or (text[0] == "~" and text[1] in ":$#")


def _decode_transit_str(text: str) -> Any:
    if text.startswith("~:"):
        return Keyword(text[2:])
    if text.startswith("~u"):
        try:
            return uuid.UUID(text[2:])
        except ValueError as exc:
            raise ValueError(f"malformed transit uuid {text!r}") from exc
    # `~~` and `~^` are escapes for a literal leading `~` / `^`. Every other
    # `~x` tag (`~m` millis, `~i` big int, …) passes through as-is: this client
    # only ever reads a handful of fields out of a response, and an unknown tag
    # in some field it ignores must not fail the whole decode.
    if text.startswith("~~") or text.startswith("~^"):
        return text[1:]
    return text


def _decode_transit(value: Any, cache: list | None = None,
                    as_map_key: bool = False) -> Any:
    """Decode transit, honouring both map forms and the read cache.

    Penpot's backend answers in transit's *compact* form -- maps arrive as
    ``["^ ", k, v, …]`` rather than as JSON objects, and a key repeated later
    in the document arrives as a back-reference like ``"^0"`` into a cache
    built in parse order. A decoder that handles only JSON-object maps returns
    a list here, and the caller's ``.get("id")`` fails on a response that was
    in fact perfectly good -- which is exactly how this was found, against the
    live instance rather than against this module's own encoder.
    """
    if cache is None:
        cache = []

    if isinstance(value, str):
        if value.startswith("^") and value != _MAP_AS_ARRAY:
            try:
                return cache[_code_to_index(value)]
            except (IndexError, ValueError) as exc:
                raise ValueError(
                    f"transit cache reference {value!r} points past the "
                    f"{len(cache)} cached value(s)") from exc
        decoded = _decode_transit_str(value)
        if _is_cacheable(value, as_map_key):
            cache.append(decoded)
        return decoded

    if isinstance(value, list):
        # A tagged value in compact form: ["~#tag", representation].
        if (len(value) == 2 and isinstance(value[0], str)
                and value[0].startswith(_TAG)):
            return _decode_transit(value[1], cache, False)
        if value and value[0] == _MAP_AS_ARRAY:
            items = value[1:]
            if len(items) % 2:
                raise ValueError(
                    "transit map-as-array has an odd number of entries")
            result: dict = {}
            # Strictly left-to-right: the cache is positional, so decoding a
            # value before its key would misalign every later reference.
            for i in range(0, len(items), 2):
                key = _decode_transit(items[i], cache, True)
                result[key] = _decode_transit(items[i + 1], cache, False)
            return result
        return [_decode_transit(v, cache, False) for v in value]

    if isinstance(value, dict):
        # A tagged value in verbose form: {"~#tag": representation}. This is
        # how the exporter hands back the asset URI -- `{"~:uri": {"~#uri":
        # "http://..."}}` -- so a decoder that leaves it wrapped gives the
        # caller a dict where it expected a string, and the export fails
        # *after* a successful multi-second render.
        if len(value) == 1:
            only_key = next(iter(value))
            if isinstance(only_key, str) and only_key.startswith(_TAG):
                return _decode_transit(value[only_key], cache, False)
        return {_decode_transit(k, cache, True): _decode_transit(v, cache, False)
                for k, v in value.items()}

    return value


def _transit_dumps(value: Any) -> str:
    return json.dumps(_encode_transit(value))


def _transit_loads(text: str) -> Any:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON (transit is JSON-hosted): {exc}") from exc
    return _decode_transit(raw)


_TRANSIT_HEADERS = {
    "content-type": "application/transit+json",
    "accept": "application/transit+json",
}


# --- the client --------------------------------------------------------


class ExporterClient:
    """A logged-in service-account session against one Penpot instance.

    Login is lazy -- the first call that needs it triggers it -- and a 401
    from either hop triggers exactly one re-login and one retry of that same
    request; a second 401 (or any other failure) is raised, never silently
    swallowed or retried into staleness.

    ``transport`` (an ``httpx.BaseTransport``, e.g. ``httpx.MockTransport``)
    or a fully-built ``client`` may be injected so tests never touch the
    network, mirroring how :mod:`awm.drawio.export` lets a fake render
    callable stand in for the real container.
    """

    def __init__(self, *, base_url: str | None = None,
                 exporter_url: str | None = None,
                 username: str | None = None, password: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 client: httpx.Client | None = None) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._exporter_url = (exporter_url or DEFAULT_EXPORTER_URL).rstrip("/")
        self._username = username if username is not None else DEFAULT_USERNAME
        self._password = password if password is not None else DEFAULT_PASSWORD
        self._client = client if client is not None else httpx.Client(transport=transport)
        self._token: str | None = None
        self._profile_id: uuid.UUID | None = None
        #: Guards the token/profile-id pair. One client is shared by every
        #: request thread and every background refresh, so both the cold
        #: login and the 401 re-login must be single-flight.
        self._auth_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ExporterClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- auth ------------------------------------------------------------

    def _login(self) -> None:
        if not self._username or not self._password:
            raise ExporterError(
                "penpot service-account credentials are not configured "
                "(PENPOT_SERVICE_USERNAME / PENPOT_SERVICE_PASSWORD)")
        url = f"{self._base_url}{LOGIN_PATH}"
        body = _transit_dumps({
            Keyword("email"): self._username,
            Keyword("password"): self._password,
        })
        try:
            resp = self._client.request(
                "POST", url, content=body, timeout=LOGIN_TIMEOUT,
                headers=_TRANSIT_HEADERS)
        except httpx.HTTPError as exc:
            raise ExporterError(f"login to {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise ExporterError(f"login to {url} failed: HTTP {resp.status_code}")
        token = resp.cookies.get(COOKIE_NAME)
        if not token:
            raise ExporterError(
                f"login to {url} returned HTTP 200 but set no "
                f"{COOKIE_NAME!r} cookie")
        try:
            profile = _transit_loads(resp.text)
        except ValueError as exc:
            raise ExporterError(
                f"login response from {url} could not be parsed as "
                f"transit: {exc}") from exc
        profile_id = profile.get("id") if isinstance(profile, Mapping) else None
        if not isinstance(profile_id, uuid.UUID):
            raise ExporterError(
                f"login response from {url} had no usable profile id: "
                f"{profile!r}")
        self._token = token
        self._profile_id = profile_id
        log.info("penpot-view: logged in to %s as service account", self._base_url)

    def _ensure_authed(self) -> str:
        """Return a token to use, logging in once if the session is empty.

        Returns the token rather than leaving the caller to re-read
        ``self._token``. This client is shared by every request thread the
        ``ThreadingHTTPServer`` runs plus every background refresh, and the
        read-then-use of a token is otherwise a race: one thread can clear it
        while handling a 401 in the window between another thread's check and
        its cookie header, which sends ``auth-token=None`` and burns that
        thread's single retry on a request that was never going to work.

        The lock is held across the login so a cold start cannot stampede
        every waiting thread into its own redundant login.
        """
        with self._auth_lock:
            if self._token is None or self._profile_id is None:
                self._login()
            return self._token

    def _authed_request(self, method: str, url: str, *, timeout: float,
                        headers: dict[str, str] | None = None,
                        retried: bool = False, **kwargs: Any) -> httpx.Response:
        token = self._ensure_authed()
        # Set explicitly per request, as a header rather than via httpx's
        # cookies= kwarg (deprecated in 0.28 and jar-scoped anyway) -- a
        # re-login must swap the token on the very next call, not merge with
        # whatever the client's jar remembered. The token comes back from
        # _ensure_authed rather than being re-read off self, so a concurrent
        # re-login cannot blank it between the check and this line.
        merged_headers = dict(headers or {})
        merged_headers["cookie"] = f"{COOKIE_NAME}={token}"
        resp = self._client.request(method, url, headers=merged_headers,
                                    timeout=timeout, **kwargs)
        if resp.status_code == 401 and not retried:
            log.warning(
                "penpot-view: %s %s got 401; auth-token stale or rejected, "
                "re-logging in once", method, url)
            with self._auth_lock:
                # Only the thread whose token was the one rejected should pay
                # for a fresh login. If another thread already replaced it
                # while this request was in flight, reuse that one instead of
                # invalidating a session that is working for everybody else.
                if self._token == token:
                    self._token = None
                    self._profile_id = None
                    self._login()
            return self._authed_request(method, url, timeout=timeout,
                                        headers=headers, retried=True, **kwargs)
        return resp

    # --- freshness ----------------------------------------------------------

    def file_etag(self, file_id: str, known: str | None = None
                  ) -> tuple[bool, str | None]:
        """Has this file changed since ``known``? Returns ``(changed, etag)``.

        Penpot instruments ``get-file`` with its conditional-loading
        middleware (``app.rpc.cond``): the RPC layer lifts ``If-None-Match``
        into ``::cond/key``, and when that key still matches, the middleware
        answers **304 with an empty body** having run only the cheap
        ``get-minimal-file-with-perms`` single-row lookup rather than loading
        the file. The key itself is built from ``revn``/``vern``/
        ``modified-at`` (``get-file-etag`` in ``rpc/commands/files.clj``), so
        it moves on a real edit. Verified live against the running stack:
        unchanged answers 304 in 0 bytes, and an edit moves the tag.

        This is what lets :mod:`awm.penpot_view.view` invalidate on change
        rather than expire on a timer. Two honest limits:

        * The tag is per **file**, not per board -- the only granularity
          Penpot exposes -- so editing any board in a file marks every
          cached board of that file for re-render. Correct, but coarser
          than the cache key.
        * A *changed* file answers 200 with the whole file body, which is
          discarded here. That cost is only paid when something actually
          changed, i.e. immediately before a far more expensive re-render.

        Never raises for a freshness question: a probe that fails answers
        "changed", so the caller re-renders rather than serving something
        stale on the strength of a failed check.
        """
        if not is_uuid(file_id):
            raise ExporterError(f"file-id {file_id!r} is not a Penpot UUID")
        url = f"{self._base_url}{RPC_PREFIX}/get-file"
        headers = {"content-type": "application/transit+json",
                   "accept": "application/transit+json"}
        if known:
            headers["if-none-match"] = known
        try:
            resp = self._authed_request(
                "POST", url, timeout=FRESHNESS_TIMEOUT, headers=headers,
                content=_transit_dumps({Keyword("id"): uuid.UUID(file_id)}))
        except httpx.HTTPError as exc:
            log.warning("penpot-view: freshness probe for %s failed (%s); "
                        "treating as changed", file_id, exc)
            return True, None
        if resp.status_code == 304:
            return False, known
        if resp.status_code != 200:
            log.warning("penpot-view: freshness probe for %s returned HTTP %s; "
                        "treating as changed", file_id, resp.status_code)
            return True, None
        return True, resp.headers.get("etag")

    # --- export ------------------------------------------------------------

    def export_svg(self, *, file_id: str, page_id: str, object_id: str,
                    name: str, scale: float = 1.0, suffix: str = "") -> bytes:
        """Render one board/shape to SVG and return the raw bytes.

        Raises :class:`ExporterError`, always naming what failed, for
        anything short of a genuine SVG payload -- a non-200 on either hop,
        the exporter's 401-once-retried case exhausted, an asset URI that
        does not point at the frontend, a 204/zero-byte/wrong-content-type
        asset response, or an expired (404/410) tempfile.
        """
        for label, value in (("file-id", file_id), ("page-id", page_id),
                             ("object-id", object_id)):
            if not is_uuid(value):
                raise ExporterError(f"{label} {value!r} is not a Penpot UUID")

        self._ensure_authed()
        uri = self._export_shapes(file_id=file_id, page_id=page_id,
                                  object_id=object_id, name=name,
                                  scale=scale, suffix=suffix)

        # The one check that stands between "fetched the render" and
        # "fetched a silent 204 from the backend" -- see the module
        # docstring. Refuse before spending a request on the wrong host.
        if not uri.startswith(self._base_url):
            raise ExporterError(
                f"asset uri {uri!r} does not start with the frontend base "
                f"{self._base_url!r} -- refusing to fetch it, since "
                "anything else risks the backend's storage layer answering "
                "a bodyless 204 that would look like a successful export")

        return self._fetch_asset(uri)

    def _export_shapes(self, *, file_id: str, page_id: str, object_id: str,
                       name: str, scale: float, suffix: str) -> str:
        url = f"{self._exporter_url}{EXPORT_PATH}"
        body = _transit_dumps({
            Keyword("cmd"): Keyword("export-shapes"),
            Keyword("profile-id"): self._profile_id,
            Keyword("exports"): [{
                Keyword("page-id"): uuid.UUID(page_id),
                Keyword("file-id"): uuid.UUID(file_id),
                Keyword("object-id"): uuid.UUID(object_id),
                Keyword("type"): Keyword("svg"),
                Keyword("suffix"): suffix,
                Keyword("scale"): scale,
                Keyword("name"): name,
            }],
            Keyword("wait"): True,
        })
        resp = self._authed_request(
            "POST", url, timeout=EXPORT_TIMEOUT, content=body,
            headers=_TRANSIT_HEADERS)
        if resp.status_code != 200:
            raise ExporterError(
                f"export-shapes for {file_id}/{page_id}/{object_id} failed: "
                f"HTTP {resp.status_code} from {url}")
        try:
            resource = _transit_loads(resp.text)
        except ValueError as exc:
            raise ExporterError(
                f"export-shapes response for {file_id}/{page_id}/{object_id} "
                f"could not be parsed as transit: {exc}") from exc
        if not isinstance(resource, Mapping):
            raise ExporterError(
                f"export-shapes response for {file_id}/{page_id}/{object_id} "
                f"was not a map: {resource!r}")
        uri = resource.get("uri")
        if not uri:
            raise ExporterError(
                f"export-shapes response for {file_id}/{page_id}/{object_id} "
                f"had no 'uri': {resource!r}")
        return uri

    def _fetch_asset(self, uri: str) -> bytes:
        resp = self._authed_request("GET", uri, timeout=ASSET_TIMEOUT,
                                    headers={"accept": "image/svg+xml"})

        # HTTP 204 + x-accel-redirect is serve-object-from-fs's signal to an
        # nginx `internal` location it does not itself have -- getting it
        # here means the request reached the backend/storage layer directly.
        # See the module docstring: this is a hard error, never an empty
        # success.
        if resp.status_code == 204:
            raise ExporterError(
                f"asset {uri} answered HTTP 204 with no body -- this is "
                "serve-object-from-fs's x-accel-redirect signal (see "
                "backend/src/app/http/assets.clj), meaning the request "
                "reached Penpot's storage layer directly instead of being "
                "resolved by the frontend nginx's internal /internal/assets "
                "location; fetch the asset through the frontend, never the "
                "backend")
        if resp.status_code in (404, 410):
            raise ExporterError(
                f"asset {uri} is gone (HTTP {resp.status_code}) -- likely "
                "past Penpot's 10-minute tempfile expiry; this is a hard "
                "failure to surface, not something to retry into staleness")
        if resp.status_code != 200:
            raise ExporterError(f"asset {uri} fetch failed: HTTP {resp.status_code}")

        content_type = resp.headers.get("content-type", "")
        if not content_type.startswith("image/svg+xml"):
            raise ExporterError(
                f"asset {uri} content-type is {content_type!r}, not "
                "image/svg+xml -- tempfile is not in public-buckets, so an "
                "unauthenticated or otherwise wrong response here shows up "
                "as an attachment download, not SVG bytes; refusing to hand "
                "it back as a render")

        data = resp.content
        if not data:
            raise ExporterError(f"asset {uri} returned zero bytes")
        return data

    # --- sub-resources --------------------------------------------------

    def fetch_subresource(self, url: str) -> tuple[str, bytes]:
        """Fetch one sub-resource an exported SVG still points at, so it can
        be inlined (see :func:`awm.penpot_view.svgpost.inline_externals`).

        Penpot's exported SVG is **not** self-contained: image fills arrive as
        ``<image href="{PENPOT_PUBLIC_URI}/assets/by-file-media-id/...">`` and
        every web font as ``url({PENPOT_PUBLIC_URI}/internal/gfonts/...)``.
        Both are absolute URLs against Penpot's *own* public origin, and the
        image ones are authenticated -- an unauthenticated GET answers 404.
        A viewer that is not logged into Penpot, or that reaches this service
        from anywhere Penpot's origin does not resolve, therefore gets a
        render with holes where its images should be and fallback fonts in
        place of its real ones, with no error anywhere. Inlining is what
        turns the export into something that renders the same for everyone.

        **Same-origin only, and that restriction is load-bearing.** The URL
        comes out of a document a Penpot user authored, and this method
        attaches the service account's cookie to it. Fetching an arbitrary
        URL from it would let any board hand this service an internal address
        and read the response back out of its own render -- so anything that
        is not Penpot's own origin is refused here rather than filtered
        further up.
        """
        parsed = httpx.URL(url)
        base = httpx.URL(self._base_url)
        if (parsed.scheme, parsed.host, parsed.port) != (base.scheme, base.host, base.port):
            raise ExporterError(
                f"refusing to fetch sub-resource {url!r}: not same-origin "
                f"with {self._base_url} -- the service session's cookie is "
                "never sent anywhere but Penpot itself")

        resp = self._authed_request("GET", url, timeout=SUBRESOURCE_TIMEOUT)
        if resp.status_code != 200:
            raise ExporterError(
                f"sub-resource {url} fetch failed: HTTP {resp.status_code}")
        data = resp.content
        if not data:
            raise ExporterError(f"sub-resource {url} returned zero bytes")
        if len(data) > MAX_SUBRESOURCE_BYTES:
            raise ExporterError(
                f"sub-resource {url} is {len(data)} bytes, over the "
                f"{MAX_SUBRESOURCE_BYTES}-byte inlining cap")
        content_type = (resp.headers.get("content-type", "")
                        .split(";")[0].strip() or "application/octet-stream")
        return content_type, data
