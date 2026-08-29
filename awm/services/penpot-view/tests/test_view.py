"""The on-demand board-view listener: caching, freshness, and the connection
hazards a 2026-08-25 production incident already found on the sibling drawio
listener this mirrors.

Every scenario pinned here is a way the response could be wrong while looking
fine, or a way one bad request could corrupt what a *different* request sees:
a cache slot shared across boards, a stale-fetch trigger silently dropped, a
concurrent cold-miss splicing two renders together, a body byte left on a
reused connection, or a real exporter failure reaching the caller as a raw
traceback instead of a 502. No network and no live Penpot -- a fake stands in
for :class:`~awm.penpot_view.exporter_client.ExporterClient`, exactly the way
:mod:`test_exporter_client` fakes the transport underneath it.
"""

from __future__ import annotations

import threading
import time
from http.client import HTTPConnection

import pytest

from awm.penpot_view import view as V
from awm.penpot_view.exporter_client import ExporterError

FILE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ab"
PAGE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ac"
BOARD_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ad"
BOARD_ID_2 = "0197f9d2-1a2b-73aa-8b9c-1234567890ae"

SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'

PATH = f"/penpot-view/{FILE_ID}/{PAGE_ID}/{BOARD_ID}"
PATH_2 = f"/penpot-view/{FILE_ID}/{PAGE_ID}/{BOARD_ID_2}"


class FakeExporter:
    """Stands in for ExporterClient -- exposes only ``export_svg``, matching
    its keyword-only signature so a swapped-in real client needs no call-site
    change here."""

    def __init__(self, *, svg: bytes = SVG_BYTES, error: Exception | None = None,
                delay: float = 0.0):
        self.svg = svg
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, str, str, float]] = []
        self._lock = threading.Lock()

    def export_svg(self, *, file_id, page_id, object_id, name, scale=1.0,
                   suffix=""):
        with self._lock:
            self.calls.append((file_id, page_id, object_id, scale))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.svg


class _Harness:
    def __init__(self, renderer: V.Renderer, exporter: FakeExporter,
                httpd, thread: threading.Thread):
        self.renderer = renderer
        self.exporter = exporter
        self.httpd = httpd
        self.thread = thread
        self.port = httpd.server_address[1]

    def request(self, method: str, path: str, *, headers=None, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp, data
        finally:
            conn.close()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def harness(tmp_path):
    """Spin a real penpot-view listener over a fake exporter.

    A large default TTL (far beyond any test's own runtime) keeps a test's
    wall-clock timing from ever triggering a background refresh that would
    make its exporter-call-count assertions flaky -- freshness itself is not
    what most of these tests are about.
    """
    made: list[_Harness] = []

    def _make(exporter: FakeExporter | None = None, ttl: float = 300.0):
        exporter = exporter or FakeExporter()
        renderer = V.Renderer(exporter, cache_dir=tmp_path / str(len(made)),
                              ttl=ttl)
        handler = V._make_handler(renderer)
        httpd = V.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        h = _Harness(renderer, exporter, httpd, thread)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.stop()


# --- path/spec parsing --------------------------------------------------

def test_path_of_the_wrong_shape_is_a_400():
    with pytest.raises(V.ViewError, match="expected"):
        V.parse_path(f"{V.VIEW_PREFIX}/{FILE_ID}")


def test_a_non_uuid_path_segment_is_a_400():
    with pytest.raises(V.ViewError, match="board-id"):
        V.parse_path(f"{V.VIEW_PREFIX}/{FILE_ID}/{PAGE_ID}/not-a-uuid")


def test_a_path_outside_the_prefix_is_a_404():
    with pytest.raises(V.ViewError) as excinfo:
        V.parse_path("/somewhere-else")
    assert excinfo.value.status == 404


# --- the happy path ------------------------------------------------------

def test_a_cold_miss_renders_and_returns_the_svg(harness):
    """The first request for a board has nothing to serve but a real render
    -- this is the one case this cache is allowed to block on."""
    h = harness()
    resp, data = h.request("GET", PATH)
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "image/svg+xml"
    assert resp.getheader("Content-Length") == str(len(data))
    assert data == SVG_BYTES
    assert len(h.exporter.calls) == 1


def test_second_request_is_a_cache_hit_and_does_not_reinvoke_the_exporter(harness):
    h = harness()
    h.request("GET", PATH)
    resp, data = h.request("GET", PATH)
    assert resp.status == 200
    assert data == SVG_BYTES
    assert len(h.exporter.calls) == 1


def test_matching_if_none_match_returns_304_with_no_body(harness):
    """Cache-Control: no-cache means the client always revalidates -- this is
    what lets that revalidation cost a cheap 304 instead of a full render."""
    h = harness()
    resp1, _ = h.request("GET", PATH)
    etag = resp1.getheader("ETag")
    assert etag

    resp2, data2 = h.request("GET", PATH, headers={"If-None-Match": etag})
    assert resp2.status == 304
    assert data2 == b""
    assert len(h.exporter.calls) == 1


# --- refusing a bad parameter rather than degrading silently -------------

def test_a_blank_swap_query_is_a_400_not_a_silent_plain_render(harness):
    """`parse_qs` drops blank values by default, which would render the plain
    board and call it success; the handler keeps them precisely so
    `renderspec.from_query` can refuse `?swap=` instead."""
    h = harness()
    resp, data = h.request("GET", f"{PATH}?swap=")
    assert resp.status == 400
    assert b"swap" in data.lower()
    assert h.exporter.calls == []


# --- a real export failure is a 502, never a traceback --------------------

def test_an_exporter_error_surfaces_as_502_with_a_reason(harness):
    exporter = FakeExporter(error=ExporterError("login to penpot failed: boom"))
    h = harness(exporter=exporter)
    resp, data = h.request("GET", PATH)
    assert resp.status == 502
    assert b"boom" in data


# --- cache key scoping -----------------------------------------------------

def test_the_cache_key_is_per_board_not_shared_across_boards(harness):
    """A composite board's render must not be forced to re-render, or share a
    cache slot with, a different board on the same file -- exactly what
    renderspec.cache_key is scoped to guarantee."""
    h = harness()
    h.request("GET", PATH)
    h.request("GET", PATH_2)
    assert len(h.exporter.calls) == 2
    assert h.exporter.calls[0][2] == BOARD_ID
    assert h.exporter.calls[1][2] == BOARD_ID_2

    # Each board's own cache hit still works independently afterwards.
    h.request("GET", PATH)
    h.request("GET", PATH_2)
    assert len(h.exporter.calls) == 2


# --- concurrency safety ------------------------------------------------

def test_concurrent_cold_requests_for_one_key_do_not_corrupt_the_cached_bytes(harness):
    """A ThreadingHTTPServer means concurrent first-requests for one board
    routinely overlap in real deployments. Only one render may actually run,
    and every caller -- including the ones that only joined it -- must get
    the exact same, uncorrupted bytes, not a splice from two interleaved
    writes sharing a cache slot."""
    exporter = FakeExporter(delay=0.25)
    h = harness(exporter=exporter)
    results: list[tuple[int, bytes]] = []
    lock = threading.Lock()

    def worker():
        resp, data = h.request("GET", PATH)
        with lock:
            results.append((resp.status, data))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 16
    assert all(status == 200 for status, _ in results)
    assert all(data == SVG_BYTES for _, data in results)
    assert len(exporter.calls) == 1


# --- the drain hazard ----------------------------------------------------

def test_a_declared_content_length_body_is_drained_and_the_connection_survives(harness):
    """A GET body is never expected here, but a client (or a proxy in front
    of one) can send one on a connection meant to be reused. Leaving those
    bytes unread desynchronises the next request on the same socket -- the
    exact shape of the 2026-08-25 incident this handler's drain guards
    against (there it was a phantom chunked-encoding terminator instead of a
    declared body, but the failure mode -- a stdlib handler that never reads
    `rfile` -- is the same one being pinned here)."""
    h = harness()
    conn = HTTPConnection("127.0.0.1", h.port, timeout=15)
    try:
        conn.request("GET", PATH, body=b"x" * 256)
        resp1 = conn.getresponse()
        data1 = resp1.read()
        assert resp1.status == 200
        assert data1 == SVG_BYTES

        # If the body had been left on the wire, this second request on the
        # SAME connection would desynchronise and get back garbage or nothing.
        conn.request("GET", PATH)
        resp2 = conn.getresponse()
        data2 = resp2.read()
        assert resp2.status == 200
        assert data2 == SVG_BYTES
    finally:
        conn.close()


# --- svgpost problems are visible, not swallowed ----------------------------

def test_svgpost_problems_reach_a_response_header_and_the_log(harness, caplog):
    """A degraded picture is acceptable; a *silently* degraded one is not.
    A paint spelled in a form `swap` cannot parse -- a CSS colour name --
    must be visible to the caller via a header, and logged, rather than
    folded into an ordinary 200."""
    problem_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<rect fill="rebeccapurple"/></svg>'
    )
    exporter = FakeExporter(svg=problem_svg)
    h = harness(exporter=exporter)

    with caplog.at_level("WARNING", logger="awm.penpot_view.view"):
        resp, data = h.request("GET", f"{PATH}?swap=663399:00ff00")

    assert resp.status == 200
    assert resp.getheader("X-Penpot-Problems") == "1"
    assert any("problem" in rec.message.lower() for rec in caplog.records)


def test_a_degraded_render_is_still_reportable_after_the_request(harness):
    """The header answers the request that provoked the problem and nobody
    else. A person meeting the picture later -- in a note, where no header is
    visible -- needs the service itself to be able to say so, which is what
    `status` reads.
    """
    problem_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<rect fill="rebeccapurple"/></svg>'
    )
    h = harness(exporter=FakeExporter(svg=problem_svg))

    assert h.renderer.degraded() == []

    resp, _ = h.request("GET", f"{PATH}?swap=663399:00ff00")
    assert resp.status == 200

    degraded = h.renderer.degraded()
    assert len(degraded) == 1
    assert degraded[0]["problems"]
    assert degraded[0]["board_id"]


def test_a_clean_render_reports_nothing_degraded(harness):
    """Guards the other direction: a `degraded` that reported every entry
    would be as useless as one that reported none, and would pass a test
    that only ever renders a broken board."""
    h = harness()
    resp, _ = h.request("GET", PATH)
    assert resp.status == 200
    assert h.renderer.degraded() == []


# --- the public server class, not just the handler factory -----------------

def test_viewserver_start_listener_serves_real_requests(tmp_path):
    """The actual entrypoint another module constructs -- confirm the wiring
    from ViewServer down through the handler factory is correct, not just
    `_make_handler` in isolation."""
    exporter = FakeExporter()
    renderer = V.Renderer(exporter, cache_dir=tmp_path, ttl=300.0)
    server = V.ViewServer(renderer)
    port = server.start_listener()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.request("GET", PATH)
            resp = conn.getresponse()
            data = resp.read()
            assert resp.status == 200
            assert data == SVG_BYTES
        finally:
            conn.close()
    finally:
        server._httpd.shutdown()
        server._httpd.server_close()


# --- the cache's own dirty/chain contract, without going through HTTP ------

def test_a_stale_trigger_arriving_mid_refresh_is_chained_not_dropped():
    """The "don't join a stale fetch" hazard, exercised directly against
    Cache: a second staleness trigger arriving while a background refresh is
    already running for the same key must not be silently absorbed into that
    running fetch -- it must cause one more render once the first completes,
    so the entry ends up reflecting the later trigger rather than looking
    fresh off a render that started before it."""
    release = threading.Event()
    calls = []
    call_lock = threading.Lock()

    def render():
        with call_lock:
            n = len(calls) + 1
            calls.append(n)
        if n == 1:
            release.wait(timeout=5)  # hold the first render open
        return (f"<svg>{n}</svg>".encode(), [])

    cache = V.Cache(ttl=0.0)  # ttl=0 -> every get() sees the entry as stale
    key = (FILE_ID, PAGE_ID, BOARD_ID, "__plain__")

    # Prime the entry with a first, fast synchronous render (cold miss).
    def first_render():
        return (b"<svg>0</svg>", [])
    cache.get(key, first_render)

    # Now trigger a background refresh (entry is stale, ttl=0.0) that blocks
    # inside `render` until `release` is set.
    entry = cache._entry_for(key)
    cache._trigger_refresh(key, entry, render)
    # Give the background thread a moment to actually start and register
    # itself as the in-flight fetch before the second trigger below.
    for _ in range(100):
        if entry.fetch is not None:
            break
        time.sleep(0.01)
    assert entry.fetch is not None

    # A second trigger arrives while the first is still running: must not
    # start a concurrent second render, and must not be dropped either.
    cache._trigger_refresh(key, entry, render)
    assert entry.dirty is True

    release.set()  # let the first background render finish
    for _ in range(200):
        with call_lock:
            done = len(calls) >= 2
        if done and entry.fetch is None:
            break
        time.sleep(0.01)

    assert len(calls) == 2  # the chained render actually ran
    assert entry.dirty is False
    assert entry.data == b"<svg>2</svg>"  # reflects the later render, not the first


# --- invalidate on change ------------------------------------------------
#
# Penpot's get-file answers 304 in zero bytes when the file has not moved, so
# the TTL is a rate limit on *asking*, not an expiry. These pin the four ways
# that can go wrong -- three of which end in a permanently stale picture.

def _counting_render(body: bytes = b"<svg/>"):
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return body, []
    return render, calls


def test_an_unchanged_source_file_is_not_rerendered_even_past_the_ttl(tmp_path):
    """The whole point of the probe: a board nobody touched must not re-render
    on a timer. With a bare TTL this is exactly what did happen."""
    probes = {"n": 0}

    def freshness(file_id, known):
        probes["n"] += 1
        return False, "W/\"same\""

    cache = V.Cache(tmp_path, ttl=0.0, freshness=freshness)
    render, calls = _counting_render()
    key = ("f", "p", "b", "__plain__")

    cache.get(key, render)
    for _ in range(5):
        cache.get(key, render)
    assert calls["n"] == 1, "an unchanged file must never re-render"
    assert probes["n"] >= 1, "but it must actually be asked"


def test_a_changed_source_file_is_rerendered(tmp_path):
    """The other half -- a real edit must not wait out a timer."""
    state = {"etag": "W/\"v1\""}

    def freshness(file_id, known):
        return known != state["etag"], state["etag"]

    cache = V.Cache(tmp_path, ttl=0.0, freshness=freshness)
    render, calls = _counting_render()
    key = ("f", "p", "b", "__plain__")

    cache.get(key, render)
    assert calls["n"] == 1
    cache.get(key, render)          # unchanged -> no new render
    assert calls["n"] == 1

    state["etag"] = "W/\"v2\""      # the edit
    cache.get(key, render)
    for _ in range(20):             # the refresh is a background thread
        if calls["n"] > 1:
            break
        time.sleep(0.05)
    assert calls["n"] == 2, "an edited file must re-render"


def test_the_source_etag_is_recorded_before_the_render_not_after(tmp_path):
    """The stale-forever trap. If an edit lands *during* a render and we
    stamped the post-edit tag onto the pre-edit bytes we just produced, the
    next probe answers "unchanged" and that entry serves a stale picture
    forever -- with the TTL demoted to a rate limit, nothing rescues it."""
    seen: list[str | None] = []
    state = {"etag": "W/\"before\""}

    def freshness(file_id, known):
        seen.append(state["etag"])
        return known != state["etag"], state["etag"]

    def render():
        state["etag"] = "W/\"during-render\""   # the racing edit
        return b"<svg/>", []

    cache = V.Cache(tmp_path, ttl=0.0, freshness=freshness)
    key = ("f", "p", "b", "__plain__")
    cache.get(key, render)

    entry = cache._entries[key]
    assert entry.source_etag == "W/\"before\"", (
        "recorded the tag from after the render; the edit that landed "
        "mid-render is now invisible forever")


def test_a_failing_probe_rerenders_rather_than_trusting_the_cache(tmp_path):
    """A freshness check must never be able to pin a stale render in place,
    so every failure path answers 'changed'."""
    def freshness(file_id, known):
        raise RuntimeError("penpot unreachable")

    cache = V.Cache(tmp_path, ttl=0.0, freshness=freshness)
    render, calls = _counting_render()
    key = ("f", "p", "b", "__plain__")

    cache.get(key, render)
    cache.get(key, render)
    for _ in range(20):
        if calls["n"] > 1:
            break
        time.sleep(0.05)
    assert calls["n"] == 2, "a broken probe must not freeze the cache"


def test_with_no_probe_the_cache_degrades_to_plain_ttl(tmp_path):
    """The weaker behaviour has to stay available and explicit -- but it is a
    constructor argument, not a silent default."""
    cache = V.Cache(tmp_path, ttl=0.0)
    render, calls = _counting_render()
    key = ("f", "p", "b", "__plain__")

    cache.get(key, render)
    cache.get(key, render)
    for _ in range(20):
        if calls["n"] > 1:
            break
        time.sleep(0.05)
    assert calls["n"] == 2


# --- findings from the first adversarial review ---------------------------

def test_a_slow_probe_cannot_stamp_a_newer_etag_onto_older_bytes(tmp_path):
    """The lost-update race that pins a render stale forever.

    Two requests overlap. Both read source_etag='v1' and probe. The fast one
    sees 'v2', renders, and seeds 'v2'. The file then moves to 'v3'. The slow
    probe finally returns 'v3' -- and if _changed wrote that back, the entry
    would claim to hold v3 while actually holding v2. Every later probe would
    answer 304 and the entry would serve v2 forever, with the TTL demoted to
    a rate limit and nothing left to rescue it.
    """
    cache = V.Cache(tmp_path, ttl=0.0, freshness=lambda f, k: (True, "v3"))
    key = ("f", "p", "b", "__plain__")
    entry = cache._entry_for(key)
    with entry.lock:
        entry.data, entry.etag, entry.problems = b"<svg/>", "e", ()
        entry.source_etag = "v2"          # what the cached bytes really are
        entry.rendered_at = entry.checked_at = time.monotonic()

    assert cache._changed(key, entry, "v1") is True
    assert entry.source_etag == "v2", (
        "a probe stamped its own etag onto bytes it did not render; that "
        "entry can never be seen as stale again")


def test_a_failed_warm_refresh_does_not_sit_out_a_whole_ttl(tmp_path):
    """The probe already said 'changed' and already stamped checked_at, so a
    render that then fails would otherwise leave known-stale bytes unexamined
    until a full TTL had passed."""
    def render():
        raise ExporterError("exporter down")

    cache = V.Cache(tmp_path, ttl=999.0, freshness=lambda f, k: (True, "v2"))
    key = ("f", "p", "b", "__plain__")
    entry = cache._entry_for(key)
    with entry.lock:
        entry.data, entry.etag, entry.problems = b"<svg/>", "e", ()
        entry.rendered_at = entry.checked_at = time.monotonic()

    fetch = V._Fetch()
    cache._run_fetch(key, entry, fetch, render)
    assert entry.checked_at == 0.0, "the entry must be re-probed on the next request"


def test_a_render_invalidated_mid_flight_does_not_clobber_its_replacement(tmp_path):
    """force_refresh retires a slot while a render for it is still running.
    That orphaned render must not write its now-obsolete bytes over the
    replacement's, leaving the durability copy disagreeing with what is
    served."""
    cache = V.Cache(tmp_path, ttl=0.0)
    key = ("f", "p", "b", "__plain__")
    entry = cache._entry_for(key)

    cache._persist(key, b"<svg>replacement</svg>")
    cache.invalidate(key)                       # retires `entry`
    cache._persist(key, b"<svg>orphan</svg>", entry)

    assert cache._cache_file(key).read_bytes() == b"<svg>replacement</svg>"


def test_the_entry_table_is_bounded(tmp_path, monkeypatch):
    """The variant space is caller-controlled -- every swap/crop combination
    anyone asks for mints a key -- so an unbounded table is a slow leak that
    only shows up in a long-lived process."""
    monkeypatch.setattr(V, "MAX_ENTRIES", 4)
    cache = V.Cache(tmp_path, ttl=0.0)
    for i in range(20):
        cache._entry_for(("f", "p", "b", f"variant-{i}"))
    assert len(cache._entries) == 4


def test_eviction_keeps_the_most_recently_used(tmp_path, monkeypatch):
    """Least-recently-used goes first, so a hot board is not evicted by a
    burst of one-off variants."""
    monkeypatch.setattr(V, "MAX_ENTRIES", 3)
    cache = V.Cache(tmp_path, ttl=0.0)
    hot = ("f", "p", "b", "hot")
    cache._entry_for(hot)
    for i in range(3):
        cache._entry_for(("f", "p", "b", f"cold-{i}"))
        cache._entry_for(hot)          # keep touching the hot one
    assert hot in cache._entries
