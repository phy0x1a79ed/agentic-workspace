"""The view layer: a page's live SVG at a stable URL.

The render itself needs a browser, so it is injected — every test here drives
the real path resolution, the content-hash cache, the cache pruning a
rename/removal orphans, and the change-event emit. What is worth pinning is the
behaviour a naive version gets wrong: re-rendering a page that did not change,
serving a half-document for a gone page, leaving a renamed page's render behind
forever, or failing a commit because a fan-out subscriber raised.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.drawio import view as V
from awm.drawio.store import Store
from awm.drawio.view import Renderer, ViewError, ViewNotifier

from test_checkout import TEMPLATE, set_value

SAVE = "scadc/demo.drawio"

# Two pages, each with an editable cell, so editing one page can be shown NOT to
# invalidate the other's cache (per-page content keying).
TWO_PAGES = TEMPLATE.replace(
    "</mxfile>",
    """  <diagram id="p2" name="Second">
    <mxGraphModel grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="s" value="S" vertex="1" parent="1">
          <mxGeometry x="0" y="0" width="80" height="40" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>""",
)


class FakeRender:
    """Stands in for the export container + browser. Counts renders."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.payload = b"<svg>one</svg>"

    def __call__(self, xml, fmt="pdf", *, page=None, scale=1.0, inline=True):
        self.calls.append((fmt, page, scale, inline))
        return self.payload, []


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "diagrams")
    store.create(SAVE, author="tester", xml=TWO_PAGES)
    return store


@pytest.fixture()
def render():
    return FakeRender()


@pytest.fixture()
def renderer(tmp_path, store, render):
    return Renderer(store, cache_dir=tmp_path / "viewcache", render=render)


def run(coro):
    return asyncio.run(coro)


# --- path resolution -------------------------------------------------------

def test_last_segment_is_the_page(renderer):
    assert renderer.resolve_target("scadc/demo/Second") == (SAVE, "Second")


def test_trailing_svg_is_stripped(renderer):
    assert renderer.resolve_target("scadc/demo/Second.svg") == (SAVE, "Second")


def test_page_omitted_resolves_whole_document(renderer):
    assert renderer.resolve_target("scadc/demo") == (SAVE, None)


def test_encoded_page_name(store, tmp_path, render):
    store.create("fig/spaces.drawio", author="t", xml=TEMPLATE.replace(
        'name="Page-1"', 'name="My Page"'))
    r = Renderer(store, cache_dir=tmp_path / "vc", render=render)
    assert r.resolve_target("fig/spaces/My%20Page") == ("fig/spaces.drawio",
                                                         "My Page")


def test_unknown_path_is_404(renderer):
    with pytest.raises(ViewError) as exc:
        renderer.render("scadc/ghost/Page-1")
    assert exc.value.status == 404


def test_unknown_page_is_404(renderer):
    with pytest.raises(ViewError) as exc:
        renderer.render("scadc/demo/NoSuchPage")
    assert exc.value.status == 404


def test_view_url_round_trips_through_the_renderer(renderer, store, tmp_path):
    """The verb the UI copies from and the renderer that answers the copied URL
    have to agree on encoding — otherwise a paste places an image that 404s."""
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    store.create("fig/odd.drawio", author="t",
                 xml=TWO_PAGES.replace('name="Second"', 'name="a/b;c"'))
    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))

    for save, page in [(SAVE, "Second"), (SAVE, None), ("fig/odd.drawio", "a/b;c")]:
        url = svc.view_url(save, page=page)["url"]
        rel = url[len(V.VIEW_PREFIX):]
        assert renderer.resolve_target(rel) == (svc.view_url(save)["save"], page)


# --- cache: hit, miss, per-page invalidation -------------------------------

def test_second_request_hits_cache(renderer, render):
    first = renderer.render("scadc/demo/Page-1")
    second = renderer.render("scadc/demo/Page-1")
    assert first.cached is False and second.cached is True
    assert renderer.renders == 1  # rendered once, served twice


def test_editing_a_page_invalidates_only_that_page(renderer, store, render):
    renderer.render("scadc/demo/Page-1")
    renderer.render("scadc/demo/Second")
    assert renderer.renders == 2

    # Edit only 'Page-1' (cell 'a' lives there).
    store.write(SAVE, set_value(store.read(SAVE), "a", "edited"), author="t")

    # 'Second' is unchanged content → still a cache hit; 'Page-1' re-renders.
    assert renderer.render("scadc/demo/Second").cached is True
    assert renderer.render("scadc/demo/Page-1").cached is False
    assert renderer.renders == 3


def test_render_is_handed_inlined_xml(renderer, render):
    renderer.render("scadc/demo/Page-1")
    # inline=False: the renderer inlines once for the content key and does not
    # ask the backend to inline again.
    assert render.calls[0][3] is False


def test_etag_is_the_content_hash(renderer):
    result = renderer.render("scadc/demo/Page-1")
    assert result.etag and result.etag == renderer.render("scadc/demo/Page-1").etag


def test_result_carries_save_page_rev(renderer, store):
    result = renderer.render("scadc/demo/Second")
    assert result.save == SAVE and result.page == "Second"
    assert result.rev == store.head_rev(SAVE)


def test_resolve_meta_does_not_render(renderer, render):
    # The HEAD probe the client uses to learn the topic must not render.
    save, page, rev = renderer.resolve_meta("scadc/demo/Second")
    assert (save, page) == (SAVE, "Second") and rev
    assert renderer.renders == 0


# --- cache pruning: a gone page/diagram owns its cache ---------------------

def _page_dirs(renderer, save):
    save_dir = renderer._save_dir(save)
    return sorted(p.name for p in save_dir.iterdir()) if save_dir.is_dir() else []


def test_removing_a_page_prunes_its_cache(renderer, store):
    renderer.render("scadc/demo/Page-1")
    renderer.render("scadc/demo/Second")
    assert "Second" in _page_dirs(renderer, SAVE)

    # Drop the 'Second' page and commit; the store fires the commit hook, but
    # here we drive prune directly (the hub wires it as a subscriber).
    store.write(SAVE, TEMPLATE, author="t")   # TEMPLATE has only Page-1
    renderer.prune_for_commit(SAVE)

    dirs = _page_dirs(renderer, SAVE)
    assert "Second" not in dirs and "Page-1" in dirs


def test_removing_the_diagram_prunes_everything(renderer, store):
    renderer.render("scadc/demo/Page-1")
    assert renderer._save_dir(SAVE).is_dir()

    store.remove(SAVE, author="t")
    renderer.prune_for_commit(SAVE)
    assert not renderer._save_dir(SAVE).is_dir()


def test_whole_document_render_is_never_pruned(renderer, store):
    renderer.render("scadc/demo")           # page-omitted → __whole__
    store.write(SAVE, set_value(store.read(SAVE), "a", "x"), author="t")
    renderer.prune_for_commit(SAVE)
    assert V.WHOLE_DOC in _page_dirs(renderer, SAVE)


def test_version_cap(renderer, store, render):
    # Each edit mints a new content hash; only the newest few renders survive.
    for i in range(V.MAX_VERSIONS_PER_PAGE + 3):
        store.write(SAVE, set_value(store.read(SAVE), "a", f"v{i}"), author="t")
        renderer.render("scadc/demo/Page-1")
    page_dir = renderer._page_dir(SAVE, "Page-1")
    assert len(list(page_dir.glob("*.svg"))) == V.MAX_VERSIONS_PER_PAGE


# --- change events on commit -----------------------------------------------

def test_commit_emits_view_updated():
    async def scenario():
        events = []

        async def emit(topic, payload):
            events.append((topic, payload))

        # A fresh store on the running loop so the notifier captures it.
        import tempfile
        store = Store(tempfile.mkdtemp())
        store.create(SAVE, author="t", xml=TEMPLATE)
        ViewNotifier(emit).attach(store)

        store.write(SAVE, set_value(store.read(SAVE), "a", "moved"), author="t")
        # notify schedules the emit via call_soon_threadsafe; let it run.
        await asyncio.sleep(0.05)
        return events

    events = run(scenario())
    assert events, "a commit should emit a view-updated event"
    topic, payload = events[-1]
    assert topic == f"drawio:{SAVE}"
    assert payload["type"] == "view-updated" and payload["save"] == SAVE
    assert payload["rev"]


# --- the HTTP listener over a real socket ----------------------------------

@pytest.fixture()
def server(store, renderer):
    from awm.drawio.view import ViewServer

    srv = ViewServer(store, renderer=renderer)
    srv.start_listener()
    yield srv
    srv._httpd.shutdown()


def _get(server, path, **kw):
    import httpx

    return httpx.get(f"http://127.0.0.1:{server.port}{path}", **kw)


def test_get_returns_svg_with_content_type(server):
    r = _get(server, "/drawio-app/view/scadc/demo/Page-1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["x-drawio-save"] == SAVE
    assert r.headers["etag"]
    assert r.content == b"<svg>one</svg>"


def test_conditional_get_is_304(server):
    r1 = _get(server, "/drawio-app/view/scadc/demo/Page-1")
    etag = r1.headers["etag"]
    r2 = _get(server, "/drawio-app/view/scadc/demo/Page-1",
              headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_head_resolves_save_without_body(server):
    import httpx

    r = httpx.head(f"http://127.0.0.1:{server.port}"
                   "/drawio-app/view/scadc/demo/Second")
    assert r.status_code == 200
    assert r.headers["x-drawio-save"] == SAVE
    assert r.headers["x-drawio-page"] == "Second"
    assert r.headers["x-drawio-rev"]
    assert not r.content


def test_get_missing_is_404(server):
    assert _get(server, "/drawio-app/view/scadc/ghost/Page-1").status_code == 404


def test_a_raising_subscriber_does_not_fail_the_commit(store):
    def boom(save, rev):
        raise RuntimeError("subscriber blew up")

    marks = []
    store.subscribe(boom)
    store.subscribe(lambda save, rev: marks.append(save))

    # The write must still land and the second subscriber must still run.
    result = store.write(SAVE, set_value(store.read(SAVE), "a", "z"), author="t")
    assert result["changed"] is True
    assert marks == [SAVE]
