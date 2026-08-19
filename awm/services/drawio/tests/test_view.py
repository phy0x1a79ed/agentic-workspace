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
import hashlib

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
    """Stands in for the export container + browser. Counts renders.

    Deliberately echoes a digest of the XML it was handed rather than a
    constant: with a constant payload every cache assertion below would pass
    even if the key were wrong, because the wrong bytes and the right bytes
    would be the same bytes.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self.last_xml = ""

    def __call__(self, xml, fmt="pdf", *, page=None, scale=1.0, inline=True,
                 **kw):
        self.calls.append((fmt, page, scale, inline))
        self.kwargs.append(kw)
        self.last_xml = xml
        digest = hashlib.sha256(xml.encode("utf-8")).hexdigest()[:16]
        return f"<svg>{digest}</svg>".encode("utf-8"), []


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
    topics = {topic for topic, _ in events}
    assert f"drawio:{SAVE}" in topics
    for topic, payload in events:
        assert payload["type"] == "view-updated" and payload["save"] == SAVE
        assert payload["rev"]


def test_commit_scopes_the_notify_to_the_page_that_changed():
    async def scenario():
        events = []

        async def emit(topic, payload):
            events.append(topic)

        import tempfile
        store = Store(tempfile.mkdtemp())
        # Different authors so the second write lands as its own commit
        # rather than amending the first (see Store._can_amend) — the diff
        # needs two distinct revisions to compare.
        store.create(SAVE, author="creator", xml=TWO_PAGES)
        ViewNotifier(emit).attach(store)

        # Edit cell "a", which lives on "Page-1" only.
        store.write(SAVE, set_value(store.read(SAVE), "a", "moved"), author="editor")
        await asyncio.sleep(0.05)
        return events

    events = run(scenario())
    assert f"drawio:{SAVE}:Page-1" in events
    assert f"drawio:{SAVE}:Second" not in events


def test_a_new_page_is_reported_changed():
    async def scenario():
        events = []

        async def emit(topic, payload):
            events.append(topic)

        import tempfile
        store = Store(tempfile.mkdtemp())
        store.create(SAVE, author="creator", xml=TEMPLATE)
        ViewNotifier(emit).attach(store)

        store.write(SAVE, TWO_PAGES, author="editor")
        await asyncio.sleep(0.05)
        return events

    events = run(scenario())
    assert f"drawio:{SAVE}:Second" in events


def test_first_commit_notifies_every_page_present():
    async def scenario():
        events = []

        async def emit(topic, payload):
            events.append(topic)

        import tempfile
        store = Store(tempfile.mkdtemp())
        ViewNotifier(emit).attach(store)
        # No prior revision exists for this path — `create` is its first
        # commit — so the notifier's "no history to diff" fallback fires.
        store.create(SAVE, author="t", xml=TWO_PAGES)
        await asyncio.sleep(0.05)
        return events

    events = run(scenario())
    assert f"drawio:{SAVE}:Page-1" in events
    assert f"drawio:{SAVE}:Second" in events


def test_changed_pages_is_none_when_there_is_no_prior_revision(store):
    async def noop(topic, payload):
        pass

    notifier = ViewNotifier(noop)
    notifier.attach(store)
    # `store` fixture's `create` is the only commit for this path so far.
    assert notifier._changed_pages(SAVE, rev=store.head_rev(SAVE)) is None


def test_changed_pages_reports_only_the_page_that_differs(store):
    async def noop(topic, payload):
        pass

    notifier = ViewNotifier(noop)
    notifier.attach(store)
    result = store.write(SAVE, set_value(store.read(SAVE), "a", "moved"),
                          author="t")
    assert notifier._changed_pages(SAVE, rev=result["rev"]) == ["Page-1"]


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
    assert r.content.startswith(b"<svg>")


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


# --- variants: one page, many placements -----------------------------------

def _spec(**kw):
    from awm.drawio import renderspec

    return renderspec.from_query(kw)


def test_two_variants_of_one_page_cache_independently(renderer):
    """Each renders once and neither evicts the other — the actual fix for
    three colours of one plasmid thrashing a five-slot cache."""
    green = _spec(swap=["ff00ff:00aa55"])
    blue = _spec(swap=["ff00ff:0055aa"])

    a1 = renderer.render("scadc/demo/Page-1", spec=green)
    b1 = renderer.render("scadc/demo/Page-1", spec=blue)
    a2 = renderer.render("scadc/demo/Page-1", spec=green)
    b2 = renderer.render("scadc/demo/Page-1", spec=blue)

    assert renderer.renders == 2
    assert (a1.cached, b1.cached) == (False, False)
    assert (a2.cached, b2.cached) == (True, True)
    assert a1.etag != b1.etag


def test_a_variant_does_not_disturb_the_plain_render(renderer):
    plain = renderer.render("scadc/demo/Page-1")
    renderer.render("scadc/demo/Page-1", spec=_spec(swap=["ff00ff:00aa55"]))
    again = renderer.render("scadc/demo/Page-1")
    assert again.cached is True and again.etag == plain.etag


def test_the_plain_render_keeps_its_original_cache_path(renderer):
    renderer.render("scadc/demo/Page-1")
    page_dir = renderer._page_dir(SAVE, "Page-1")
    assert list(page_dir.glob("*.svg")), "the plain render moved; old cache orphaned"


def test_the_version_cap_applies_per_variant(renderer, store):
    spec = _spec(swap=["ff00ff:00aa55"])
    for i in range(V.MAX_VERSIONS_PER_PAGE + 3):
        store.write(SAVE, set_value(store.read(SAVE), "a", f"v{i}"), author="t")
        renderer.render("scadc/demo/Page-1", spec=spec)
        renderer.render("scadc/demo/Page-1")
    page_dir = renderer._page_dir(SAVE, "Page-1")
    variant_dir = renderer._variant_dir(SAVE, "Page-1", spec)
    assert len(list(page_dir.glob("*.svg"))) == V.MAX_VERSIONS_PER_PAGE
    assert len(list(variant_dir.glob("*.svg"))) == V.MAX_VERSIONS_PER_PAGE


def test_abandoned_variants_are_reclaimed(renderer):
    for i in range(V.MAX_VARIANTS_PER_PAGE + 4):
        renderer.render("scadc/demo/Page-1",
                        spec=_spec(swap=[f"ff00ff:0000{i:02d}"]))
    page_dir = renderer._page_dir(SAVE, "Page-1")
    kept = [p for p in page_dir.iterdir() if p.is_dir()]
    assert len(kept) == V.MAX_VARIANTS_PER_PAGE


def test_removing_a_page_prunes_its_variants_too(renderer, store):
    renderer.render("scadc/demo/Second", spec=_spec(swap=["ff00ff:00aa55"]))
    store.write(SAVE, TEMPLATE, author="t")
    renderer.prune_for_commit(SAVE)
    assert "Second" not in _page_dirs(renderer, SAVE)


def test_an_unknown_crop_name_is_404(renderer):
    with pytest.raises(ViewError) as exc:
        renderer.render("scadc/demo/Page-1", spec=_spec(crop=["ghost"]))
    assert exc.value.status == 404


# --- the handler's parameter contract --------------------------------------

def test_a_malformed_swap_is_400_naming_the_token(server):
    r = _get(server, "/drawio-app/view/scadc/demo/Page-1?swap=nope")
    assert r.status_code == 400
    assert "nope" in r.text


def test_a_blank_swap_is_400_not_a_plain_render(server):
    """`parse_qs` drops it by default; that would render the plain page and
    report success, which is the failure this whole service refuses."""
    assert _get(server, "/drawio-app/view/scadc/demo/Page-1?swap=").status_code \
        == 400


def test_head_refuses_a_url_whose_get_can_only_fail(server):
    import httpx

    r = httpx.head(f"http://127.0.0.1:{server.port}"
                   "/drawio-app/view/scadc/demo/Page-1?swap=nope")
    assert r.status_code == 400


def test_variants_have_different_etags_over_the_wire(server):
    plain = _get(server, "/drawio-app/view/scadc/demo/Page-1")
    green = _get(server, "/drawio-app/view/scadc/demo/Page-1?swap=ff00ff:00aa55")
    assert plain.status_code == green.status_code == 200
    assert plain.headers["etag"] != green.headers["etag"]
    again = _get(server, "/drawio-app/view/scadc/demo/Page-1?swap=ff00ff:00aa55",
                 headers={"If-None-Match": green.headers["etag"]})
    assert again.status_code == 304


def test_an_empty_rev_does_not_reach_the_store(server):
    assert _get(server, "/drawio-app/view/scadc/demo/Page-1?rev=").status_code \
        == 200


# --- embedding one page in another -----------------------------------------
#
# A placed page view is an origin-relative URL, so a document that keeps one is
# a picture that only works on this host. The exporter resolves it by rendering
# the page — which means a page can, in principle, ask for itself.

def _placing(url: str) -> str:
    """TWO_PAGES with cell 'a' turned into an image of `url`."""
    return TWO_PAGES.replace(
        '<mxCell id="a" value="A"',
        f'<mxCell id="a" value="A" style="shape=image;image={url};"')


def test_an_embedded_page_view_is_rendered_and_inlined(renderer, store):
    store.create("fig/host.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/scadc/demo/Second"))
    result = renderer.render("fig/host/Page-1")
    assert not result.problems
    assert renderer.renders == 2          # the host, and the page it embeds
    handed = render_xml_of(renderer)
    assert V.VIEW_PREFIX not in handed and "data:image/svg+xml," in handed


def render_xml_of(renderer):
    """The XML handed to the last render — what the browser would have seen."""
    return renderer._render.last_xml


def test_a_page_that_embeds_itself_is_reported_not_hung(renderer, store):
    store.create("fig/loop.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/fig/loop/Page-1"))
    result = renderer.render("fig/loop/Page-1")
    assert any("embeds itself" in p for p in result.problems)


def test_a_cycle_across_two_diagrams_terminates(renderer, store):
    store.create("fig/one.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/fig/two/Page-1"))
    store.create("fig/two.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/fig/one/Page-1"))
    result = renderer.render("fig/one/Page-1")
    assert any("embeds itself" in p for p in result.problems)


def test_nesting_past_the_limit_is_reported(renderer, store, monkeypatch):
    from awm.drawio import export as export_mod

    monkeypatch.setattr(export_mod, "MAX_VIEW_DEPTH", 2)
    for i in range(5):
        store.create(f"fig/d{i}.drawio", author="t",
                     xml=_placing(f"{V.VIEW_PREFIX}/fig/d{i + 1}/Page-1"))
    store.create("fig/d5.drawio", author="t", xml=TWO_PAGES)
    result = renderer.render("fig/d0/Page-1")
    assert any("nest more than" in p for p in result.problems)


def test_an_escaped_query_on_a_placed_view_survives_the_store(renderer, store):
    """The store escapes '&' into '&amp;' on write. A reference that lost every
    parameter but the first would embed the wrong colour, silently."""
    # Spelled the way drawio writes it: a literal '&' would not be well-formed.
    url = (f"{V.VIEW_PREFIX}/scadc/demo/Second"
           "?swap=ff00ff:00aa55&amp;swap=00ff00:333333")
    store.create("fig/q.drawio", author="t", xml=_placing(url))
    assert "&amp;" in store.read("fig/q.drawio")
    result = renderer.render("fig/q/Page-1")
    assert not result.problems and renderer.renders == 2


def test_check_reports_a_placed_view_whose_page_is_gone(store, tmp_path):
    """`check` gates `export`; a checker blind to view references would pass an
    export with a hole in it."""
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    store.create("fig/host.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/scadc/demo/Ghost"))
    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))
    report = svc.check("fig/host.drawio")
    assert report["ok"] is False
    assert report["problems"][0]["problem"] == "view"
    assert "Ghost" in report["problems"][0]["fix"]


def test_check_accepts_a_placed_view_that_resolves(store, tmp_path):
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    store.create("fig/host.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/scadc/demo/Second"))
    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))
    report = svc.check("fig/host.drawio")
    assert report["ok"] is True and report["references"] == 1


def test_check_reports_a_malformed_parameter(store, tmp_path):
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    store.create("fig/host.drawio", author="t",
                 xml=_placing(f"{V.VIEW_PREFIX}/scadc/demo/Second?swap=nope"))
    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))
    assert svc.check("fig/host.drawio")["ok"] is False


def test_view_url_builds_the_query_the_renderer_answers(store, tmp_path,
                                                        renderer):
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))
    url = svc.view_url(SAVE, page="Second", swaps=["#F0F:00aa55"])["url"]
    assert url.endswith("?swap=ff00ff:00aa55")
    rel, query = V.split_view_url(url)
    assert renderer.render(rel, spec=_spec(**query)).save == SAVE


def test_view_url_refuses_a_typo_where_it_is_written(store, tmp_path):
    from awm.drawio import renderspec
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    svc = Service(store, Checkouts(store, tmp_path / "checkouts"))
    with pytest.raises(renderspec.SpecError):
        svc.view_url(SAVE, page="Second", swaps=["ff00ff:notacolour"])
    with pytest.raises(renderspec.CropNotFound):
        svc.view_url(SAVE, page="Second", crop="ghost")


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
