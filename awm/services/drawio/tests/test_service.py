"""The verb surface: editing operations, editor saves, and landing a merge.

The scenarios that matter here are the ones involving a live browser tab —
where a naive implementation either lets an in-flight autosave revert a merge,
or lets an actively-editing person starve an agent forever.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.drawio.checkout import Behind, Checkouts, Conflicted
from awm.drawio.ops import OpError
from awm.drawio.service import Service
from awm.drawio.store import Store

from test_checkout import TEMPLATE, set_value


@pytest.fixture()
def svc(tmp_path):
    emitted: list = []

    async def emit(topic, payload):
        emitted.append((topic, payload))

    store = Store(tmp_path / "diagrams")
    service = Service(store, Checkouts(store, tmp_path / "checkouts"), emit=emit)
    service.emitted = emitted  # type: ignore[attr-defined]
    store.create("fig/demo.drawio", author="tester", xml=TEMPLATE)
    return service


SAVE = "fig/demo.drawio"


# --- editing operations ----------------------------------------------------

def test_edit_applies_operations(svc):
    handle = svc.checkout(SAVE, author="agent")["handle"]
    result = svc.edit(handle, [
        {"op": "add_node", "page": "Page-1", "id": "mol/glucose",
         "label": "glucose", "right_of": "a", "gap": 60},
        {"op": "add_edge", "page": "Page-1", "id": "mol/e1",
         "from": "mol/glucose", "to": "a"},
    ])
    assert result["count"] == 2
    xml = svc.read(handle=handle)["xml"]
    assert 'id="mol/glucose"' in xml and 'id="mol/e1"' in xml


def test_edit_is_idempotent(svc):
    """Re-running a build must update, not duplicate — agents re-run constantly."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    build = [{"op": "add_node", "page": "Page-1", "id": "mol/x", "label": "v1"}]
    svc.edit(handle, build)
    svc.edit(handle, [{**build[0], "label": "v2"}])
    xml = svc.read(handle=handle)["xml"]
    assert xml.count('id="mol/x"') == 1 and 'value="v2"' in xml


def test_edit_never_touches_unnamed_cells(svc):
    """The property that lets an agent edit a diagram someone else drew."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    before = svc.read(handle=handle)["xml"]
    svc.edit(handle, [{"op": "add_node", "page": "Page-1", "id": "new", "label": "n"}])
    after = svc.read(handle=handle)["xml"]
    for line in before.splitlines():
        if 'id="a"' in line or 'id="b"' in line:
            assert line in after


def test_edit_is_all_or_nothing(svc):
    """A half-applied build is worse than a rejected one: the agent cannot tell
    which half landed."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    before = svc.read(handle=handle)["xml"]
    with pytest.raises(OpError):
        svc.edit(handle, [
            {"op": "add_node", "page": "Page-1", "id": "ok", "label": "fine"},
            {"op": "set", "id": "does-not-exist", "label": "boom"},
        ])
    assert svc.read(handle=handle)["xml"] == before


def test_edit_rejects_unknown_operation(svc):
    handle = svc.checkout(SAVE, author="agent")["handle"]
    with pytest.raises(OpError, match="unknown operation"):
        svc.edit(handle, [{"op": "teleport", "id": "a"}])


def test_edit_output_stays_canonical(svc):
    """If dwg wrote raw ElementTree output, every agent edit would diff against
    the browser's spelling and update would be a wall of phantom conflicts."""
    from awm.drawio import xmlmodel

    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "add_node", "page": "Page-1", "id": "n", "label": "n"}])
    xml = svc.read(handle=handle)["xml"]
    assert xmlmodel.normalize(xml) == xml


def test_edit_refuses_while_conflicted(svc):
    handle = svc.checkout(SAVE, author="agent")["handle"]
    path = svc.checkouts.file_path(handle)
    path.write_text(path.read_text() + "\n<<<<<<< checkout\n", encoding="utf-8")
    with pytest.raises(Conflicted):
        svc.edit(handle, [{"op": "add_node", "id": "n", "label": "n"}])


# --- the agent-facing handle ----------------------------------------------

def test_handle_answers_path_url_and_status(svc):
    """The contract: a handle is all an agent needs, but it can always ask for
    the file to look at and the URL to see the render."""
    result = svc.checkout(SAVE, author="agent")
    handle = result["handle"]
    assert svc.path(handle)["path"].endswith("diagram.drawio")
    assert handle in svc.url(SAVE, handle=handle)["url"]
    assert svc.status(handle)["save"] == SAVE
    assert result["url"].startswith("/drawio-app/")


def test_urls_are_origin_relative(svc):
    """Baked-in hosts are why the prototype only worked on the host's loopback."""
    assert svc.url(SAVE)["url"].startswith("/")
    assert "://" not in svc.url(SAVE)["url"]


# --- the view URL ----------------------------------------------------------

def test_view_url_covers_page_and_whole_document(svc):
    whole = svc.view_url(SAVE)
    assert whole["url"] == f"/drawio-app/view/{SAVE}"
    assert whole["page"] is None

    page = svc.view_url(SAVE, page="Page-1")
    assert page["url"] == f"/drawio-app/view/{SAVE}/Page-1"
    assert page["url"].startswith("/") and "://" not in page["url"]


def test_view_url_refuses_an_unknown_page(svc):
    """Better a refusal here than a 404 the user finds after placing the image."""
    from awm.drawio.autopublish import AutoPublishError

    with pytest.raises(AutoPublishError):
        svc.view_url(SAVE, page="not-a-page")


def test_view_url_encodes_the_page_name_whole(svc):
    """A ``/`` would split into two path segments and a ``;`` would truncate the
    drawio style string the URL ends up inside — both render a blank cell."""
    svc.store.create("fig/odd.drawio", author="tester",
                     xml=TEMPLATE.replace('name="Page-1"', 'name="a/b;c"'))
    url = svc.view_url("fig/odd.drawio", page="a/b;c")["url"]
    assert url.endswith("/a%2Fb%3Bc")


def test_list_surfaces_open_checkouts_and_editors(svc):
    svc.checkout(SAVE, author="agent")
    svc.note_tab(SAVE, "tab-1")
    entry = next(d for d in svc.list()["diagrams"] if d["save"] == SAVE)
    assert len(entry["checkouts"]) == 1 and entry["editors"] == 1


def test_cells_lists_without_reading_xml(svc):
    cells = svc.cells(SAVE)["cells"]
    assert {c["id"] for c in cells} == {"a", "b"}


# --- editor saves ----------------------------------------------------------

def test_editor_save_accepted_when_current(svc):
    rev = svc.info(SAVE)["rev"]
    xml = set_value(svc.read(SAVE)["xml"], "a", "typed")
    result = svc.save_from_editor(SAVE, xml, base_rev=rev, tab_id="tab-1")
    assert result["accepted"] and 'value="typed"' in svc.read(SAVE)["xml"]


def test_stale_editor_save_is_rejected(svc):
    """The prototype's costliest bug: a tab left open across a rebuild pushed
    its stale in-memory model back and took a page from 47 cells to 2."""
    stale_rev = svc.info(SAVE)["rev"]
    stale_xml = svc.read(SAVE)["xml"]

    svc.save_from_editor(SAVE, set_value(stale_xml, "b", "rebuilt"),
                         base_rev=stale_rev, tab_id="agent-rebuild")

    result = svc.save_from_editor(SAVE, stale_xml, base_rev=stale_rev,
                                  tab_id="stale-tab")
    assert result["accepted"] is False and result["reason"] == "stale"
    assert 'value="rebuilt"' in svc.read(SAVE)["xml"]


def test_editor_save_is_rejected_while_a_merge_holds_the_lease(svc):
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc._leases.setdefault(SAVE, __import__(
        "awm.drawio.service", fromlist=["Lease"]).Lease()).acquire(handle)
    result = svc.save_from_editor(SAVE, svc.read(SAVE)["xml"],
                                  base_rev=svc.info(SAVE)["rev"], tab_id="tab-1")
    assert result["accepted"] is False and result["reason"] == "lease"


def test_editor_save_to_a_checkout_is_rev_checked_too(svc):
    """An agent editing a checkout somebody has open in the browser is the same
    race in miniature."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    stale = svc.read(handle=handle)
    svc.edit(handle, [{"op": "add_node", "id": "agent-cell", "label": "z"}])

    result = svc.save_from_editor(SAVE, stale["xml"], base_rev=stale["rev"],
                                  tab_id="tab-1", handle=handle)
    assert result["accepted"] is False and result["reason"] == "stale"
    assert "agent-cell" in svc.read(handle=handle)["xml"]


def test_scrolling_tab_does_not_churn_history(svc):
    rev = svc.info(SAVE)["rev"]
    scrolled = svc.read(SAVE)["xml"].replace(
        "<mxGraphModel", '<mxGraphModel dx="1234" dy="99"')
    result = svc.save_from_editor(SAVE, scrolled, base_rev=rev, tab_id="tab-1")
    assert result["accepted"] and result["changed"] is False
    assert svc.info(SAVE)["rev"] == rev


# --- merging against live tabs --------------------------------------------

def test_merge_flushes_and_pushes_live_tabs(svc):
    svc.note_tab(SAVE, "tab-1")
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "add_node", "id": "agent/x", "label": "x"}])

    async def scenario():
        # The tab acknowledges promptly, as a live one would.
        async def ack():
            await asyncio.sleep(0.05)
            svc.note_flush_ack(SAVE, "tab-1")

        task = asyncio.create_task(ack())
        result = await svc.merge(handle)
        await task
        return result

    result = asyncio.run(scenario())
    assert result["changed"] is True
    kinds = [payload["type"] for _, payload in svc.emitted]
    assert kinds == ["flush", "push"]


def test_merge_does_not_hang_on_a_dead_tab(svc):
    """A tab that went away without closing its socket must not block a merge —
    the tip check inside merge is the real guard, the flush only narrows the
    window."""
    from awm.drawio import service as service_mod

    svc.note_tab(SAVE, "ghost-tab")
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "add_node", "id": "agent/x", "label": "x"}])

    original = service_mod.FLUSH_TIMEOUT_S
    service_mod.FLUSH_TIMEOUT_S = 0.2
    try:
        result = asyncio.run(svc.merge(handle))
    finally:
        service_mod.FLUSH_TIMEOUT_S = original
    assert result["changed"] is True


def test_merge_refuses_drift_the_agent_should_have_seen(svc):
    """Reconciliation belongs in update, where the agent picked the moment and
    can render the result. A merge that quietly folded in changes it never
    showed anyone would be exactly the silent-guessing failure this design
    exists to avoid."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "add_node", "id": "agent/x", "label": "from agent"}])
    svc.save_from_editor(SAVE, set_value(svc.read(SAVE)["xml"], "b", "from user"),
                         base_rev=svc.info(SAVE)["rev"], tab_id="tab-1")

    with pytest.raises(Behind):
        asyncio.run(svc.merge(handle))


def test_merge_folds_in_work_the_flush_itself_produced(svc):
    """The autosave livelock: a live tab moves the tip every couple of seconds,
    so 'refuse while behind' alone would let an actively-typing person starve
    an agent forever. Work the flush *causes* is newer than anything the agent
    could have seen, so folding it in is safe — and necessary."""
    svc.note_tab(SAVE, "tab-1")
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "add_node", "page": "Page-1", "id": "agent/x",
                       "label": "from agent"}])

    async def scenario():
        async def flush_response():
            # What a live tab does on `flush`: save its pending edits, then ack.
            await asyncio.sleep(0.05)
            svc.save_from_editor(
                SAVE, set_value(svc.read(SAVE)["xml"], "b", "typed while merging"),
                base_rev=svc.info(SAVE)["rev"], tab_id="tab-1")
            svc.note_flush_ack(SAVE, "tab-1")

        task = asyncio.create_task(flush_response())
        result = await svc.merge(handle)
        await task
        return result

    result = asyncio.run(scenario())
    landed = svc.read(SAVE)["xml"]
    assert result["changed"] is True
    assert 'id="agent/x"' in landed and 'value="typed while merging"' in landed


def test_merge_reports_a_genuine_conflict_rather_than_guessing(svc):
    """Both sides move the same cell. merge sends the agent to update, update
    reports the conflict instead of picking a winner, and the agent is left
    with something it can actually fix by hand."""
    handle = svc.checkout(SAVE, author="agent")["handle"]
    svc.edit(handle, [{"op": "set", "id": "a", "move": [500, 0]}])
    svc.save_from_editor(
        SAVE,
        svc.read(SAVE)["xml"].replace('x="0" y="0" width="80"',
                                      'x="900" y="0" width="80"'),
        base_rev=svc.info(SAVE)["rev"], tab_id="tab-1")

    with pytest.raises(Behind):
        asyncio.run(svc.merge(handle))

    result = svc.update(handle)
    assert result["conflicts"] == 1
    assert "how_to_resolve" in result
    assert svc.status(handle)["conflict_markers"] > 0

    with pytest.raises(Conflicted):
        asyncio.run(svc.merge(handle))


def test_concurrent_merges_do_not_interleave(svc):
    """Two agents landing at once must serialize, not both write."""
    first = svc.checkout(SAVE, author="agent-1")["handle"]
    second = svc.checkout(SAVE, author="agent-2")["handle"]
    svc.edit(first, [{"op": "add_node", "id": "one", "label": "1"}])
    svc.edit(second, [{"op": "add_node", "id": "two", "label": "2"}])

    async def both():
        return await asyncio.gather(svc.merge(first), svc.merge(second),
                                    return_exceptions=True)

    results = asyncio.run(both())
    landed = [r for r in results if not isinstance(r, Exception)]
    assert len(landed) >= 1
    # Whatever landed is intact; nothing produced a half-written document.
    from awm.drawio import xmlmodel
    assert xmlmodel.parse(svc.read(SAVE)["xml"]) is not None


# --- image references ------------------------------------------------------

def _with_image(svc, target: str) -> None:
    """Point a cell at an image the way drawio actually spells it."""
    rev = svc.info(SAVE)["rev"]
    xml = svc.read(SAVE)["xml"].replace(
        'value="A"', f'value="A" style="shape=image;image=/files{target};"')
    svc.save_from_editor(SAVE, xml, base_rev=rev, tab_id="tab-1")


def test_check_flags_a_missing_image(svc):
    _with_image(svc, "/nope/missing.svg")
    report = svc.check(SAVE)
    assert report["ok"] is False
    assert report["problems"][0]["problem"] == "missing"


def test_check_passes_a_real_image(svc, tmp_path):
    image = tmp_path / "molecule.svg"
    image.write_text("<svg/>", encoding="utf-8")
    _with_image(svc, str(image))

    report = svc.check(SAVE)
    assert report["ok"] is True and report["references"] == 1


def test_image_reference_survives_the_style_parser(svc, tmp_path):
    """The landmine that shaped this decision: drawio splits styles on ';', so
    a `data:image/svg+xml;base64,…` URI truncates at the first ';' and the cell
    renders blank. A filesystem path has no ';', which is the whole reason
    images are referenced rather than embedded."""
    from awm.drawio.dwg import parse_style

    image = tmp_path / "molecule.svg"
    image.write_text("<svg/>", encoding="utf-8")
    _with_image(svc, str(image))

    style = dict(seg for seg in parse_style(
        f"shape=image;image=/files{image};") if seg[1] is not None)
    assert style["image"] == f"/files{image}"
    assert svc.check(SAVE)["ok"] is True


def test_check_is_clean_for_a_diagram_with_no_images(svc):
    assert svc.check(SAVE) == {"save": SAVE, "references": 0, "problems": [],
                               "ok": True}
