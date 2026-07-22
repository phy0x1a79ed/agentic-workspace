"""Autopublish links: the standing diagram → file render.

The scenarios worth having tests for are the ones where a naive implementation
does damage rather than nothing: recreating a directory somebody deleted,
overwriting a good published figure with a failed render, or turning a minute of
autosaves into thirty renders of intermediate states.

The export container is not available here, so ``render`` is injected — every
test drives the real registry, debounce loop and replace logic.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.drawio.autopublish import (
    AutoPublishError, AutoPublisher, Link, Registry,
)
from awm.drawio.store import Store

from test_checkout import TEMPLATE, set_value

SAVE = "fig/demo.drawio"

TWO_PAGES = TEMPLATE.replace(
    "</mxfile>",
    """  <diagram id="p2" name="Second">
    <mxGraphModel grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>""",
)


class FakeRender:
    """Stands in for the export container. Records calls; can be made to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.payload = b"<svg>one</svg>"
        self.problems: list[str] = []
        self.raises: Exception | None = None

    def __call__(self, xml, fmt="pdf", *, page=None, scale=1.0, inline=True):
        self.calls.append((fmt, page, scale))
        if self.raises is not None:
            raise self.raises
        return self.payload, list(self.problems)


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "diagrams")
    store.create(SAVE, author="tester", xml=TEMPLATE)
    return store


@pytest.fixture()
def render():
    return FakeRender()


@pytest.fixture()
def pub(tmp_path, store, render):
    publisher = AutoPublisher(store, tmp_path / "autopublish.json", render=render)
    publisher.attach(store)
    return publisher


@pytest.fixture()
def out(tmp_path):
    directory = tmp_path / "published"
    directory.mkdir()
    return directory


def run(coro):
    return asyncio.run(coro)


# --- creating a link -------------------------------------------------------

def test_create_publishes_immediately(pub, out, render):
    """The first render is synchronous so the caller learns it cannot render."""
    target = out / "demo.svg"
    result = run(pub.create(SAVE, str(target)))

    assert target.read_bytes() == b"<svg>one</svg>"
    assert result["first_publish"]["published"] is True
    assert result["save"] == SAVE and result["format"] == "svg"
    assert len(render.calls) == 1


def test_create_refuses_a_missing_parent_folder(pub, tmp_path):
    """Never create the directory — a folder that is gone was deleted on purpose."""
    target = tmp_path / "nope" / "demo.svg"
    with pytest.raises(AutoPublishError, match="parent folder does not exist"):
        run(pub.create(SAVE, str(target)))
    assert not target.parent.exists()
    assert pub.list()["count"] == 0


def test_create_refuses_a_mismatched_extension(pub, out):
    """A link is a standing overwrite, so the target must look like its format."""
    with pytest.raises(AutoPublishError, match="must end in .svg"):
        run(pub.create(SAVE, str(out / "demo.txt")))


def test_create_refuses_a_relative_target(pub):
    with pytest.raises(AutoPublishError, match="absolute path"):
        run(pub.create(SAVE, "demo.svg"))


def test_create_refuses_an_unknown_format(pub, out):
    with pytest.raises(AutoPublishError, match="unknown format"):
        run(pub.create(SAVE, str(out / "demo.gif"), fmt="gif"))


def test_create_refuses_an_unknown_diagram(pub, out):
    with pytest.raises(AutoPublishError, match="no diagram"):
        run(pub.create("fig/absent.drawio", str(out / "absent.svg")))


def test_two_links_cannot_share_a_target(pub, store, out):
    """Two links on one file would fight, and the loser would be invisible."""
    store.create("fig/other.drawio", author="tester", xml=TEMPLATE)
    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))
    with pytest.raises(AutoPublishError, match="already published"):
        run(pub.create("fig/other.drawio", str(target)))


def test_many_links_on_one_diagram(pub, out, render):
    """The point of the feature: as many links as you like, independent."""
    for name in ("a", "b", "c"):
        run(pub.create(SAVE, str(out / f"{name}.svg")))
    assert pub.list()["count"] == 3
    assert pub.list(SAVE)["count"] == 3
    assert all((out / f"{n}.svg").is_file() for n in "abc")


# --- pages -----------------------------------------------------------------

def test_page_is_addressed_by_name(tmp_path, render, out):
    """Named, not indexed: reordering tabs must not repoint a link."""
    store = Store(tmp_path / "diagrams")
    store.create(SAVE, author="tester", xml=TWO_PAGES)
    pub = AutoPublisher(store, tmp_path / "autopublish.json", render=render)

    run(pub.create(SAVE, str(out / "second.svg"), page="Second"))
    assert render.calls[0][1] == 1  # resolved to the second page's index


def test_unknown_page_is_refused_at_creation(tmp_path, render, out):
    """A link that can never resolve its page is a config error, caught now."""
    store = Store(tmp_path / "diagrams")
    store.create(SAVE, author="tester", xml=TWO_PAGES)
    pub = AutoPublisher(store, tmp_path / "autopublish.json", render=render)

    with pytest.raises(AutoPublishError, match="no page named"):
        run(pub.create(SAVE, str(out / "x.svg"), page="Nope"))
    assert pub.list()["count"] == 0


# --- the trigger -----------------------------------------------------------

def test_a_store_write_republishes(pub, store, out, render):
    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))
    render.payload = b"<svg>two</svg>"

    async def scenario():
        task = asyncio.create_task(pub.run())
        store.write(SAVE, set_value(TEMPLATE, "a", "edited"), author="tester")
        await asyncio.sleep(0.2)
        task.cancel()

    with _fast_debounce():
        run(scenario())

    assert target.read_bytes() == b"<svg>two</svg>"


def test_a_merge_republishes(tmp_path, out, render):
    """The path that breaks if the hook lives on Service instead of Store."""
    from awm.drawio.checkout import Checkouts
    from awm.drawio.service import Service

    store = Store(tmp_path / "diagrams")
    store.create(SAVE, author="tester", xml=TEMPLATE)
    pub = AutoPublisher(store, tmp_path / "autopublish.json", render=render)
    pub.attach(store)
    service = Service(store, Checkouts(store, tmp_path / "checkouts"))

    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))
    render.payload = b"<svg>merged</svg>"

    async def scenario():
        task = asyncio.create_task(pub.run())
        handle = service.checkout(SAVE, author="agent")["handle"]
        service.edit(handle, [{"op": "add_node", "page": "Page-1",
                               "id": "mol/x", "label": "x"}])
        await service.merge(handle)
        await asyncio.sleep(0.2)
        task.cancel()

    with _fast_debounce():
        run(scenario())

    assert target.read_bytes() == b"<svg>merged</svg>"


def test_a_burst_of_saves_renders_once(pub, store, out, render):
    """A tab autosaving every two seconds must not queue a render per save."""
    run(pub.create(SAVE, str(out / "demo.svg")))
    assert pub.renders == 1

    async def scenario():
        task = asyncio.create_task(pub.run())
        for index in range(5):
            store.write(SAVE, set_value(TEMPLATE, "a", f"v{index}"),
                        author="tester", allow_amend=False)
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)
        task.cancel()

    with _fast_debounce():
        run(scenario())

    assert pub.renders == 2  # the create, plus one for the whole burst


def test_writes_to_other_diagrams_are_ignored(pub, store, out):
    store.create("fig/other.drawio", author="tester", xml=TEMPLATE)
    run(pub.create(SAVE, str(out / "demo.svg")))

    store.write("fig/other.drawio", set_value(TEMPLATE, "a", "x"), author="t")
    assert pub._dirty == {}


def test_continuous_editing_still_publishes(pub, store, out, render):
    """A diagram never going quiet must not defer its links forever."""
    from awm.drawio import autopublish

    run(pub.create(SAVE, str(out / "demo.svg")))

    async def scenario():
        task = asyncio.create_task(pub.run())
        for index in range(12):
            store.write(SAVE, set_value(TEMPLATE, "a", f"v{index}"),
                        author="tester", allow_amend=False)
            await asyncio.sleep(0.01)
        task.cancel()

    # Quiet never arrives — writes land every 10ms and the window is 100ms —
    # so only the ceiling can fire.
    windows = autopublish.DEBOUNCE_SECONDS, autopublish.MAX_DEFER_SECONDS
    autopublish.DEBOUNCE_SECONDS, autopublish.MAX_DEFER_SECONDS = 0.1, 0.05
    try:
        run(scenario())
    finally:
        autopublish.DEBOUNCE_SECONDS, autopublish.MAX_DEFER_SECONDS = windows

    assert pub.renders > 1


# --- failure keeps the last good render ------------------------------------

def test_a_failed_render_keeps_the_published_file(pub, out, render):
    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))
    good = target.read_bytes()

    render.raises = RuntimeError("export container is stopped")
    result = run(pub.now(save=SAVE))

    assert result["published"] == 0
    assert target.read_bytes() == good
    assert pub.list()["count"] == 1  # the link survives a transient failure


def test_broken_image_references_keep_the_published_file(pub, out, render):
    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))
    good = target.read_bytes()

    render.problems = ["/files/gone.svg: no such file"]
    render.payload = b"<svg>blank</svg>"
    result = run(pub.now(save=SAVE))

    assert result["published"] == 0
    assert target.read_bytes() == good


def test_a_link_carries_no_error_field(pub, out, render):
    run(pub.create(SAVE, str(out / "demo.svg")))
    render.raises = RuntimeError("boom")
    run(pub.now(save=SAVE))

    record = pub.list()["links"][0]
    assert not any("error" in key or "health" in key for key in record)


def test_a_failed_render_leaves_no_temp_file(pub, out, render):
    run(pub.create(SAVE, str(out / "demo.svg")))
    render.raises = RuntimeError("boom")
    run(pub.now(save=SAVE))

    assert sorted(p.name for p in out.iterdir()) == ["demo.svg"]


# --- the destination owns the link's life ----------------------------------

def test_a_vanished_parent_folder_deletes_the_link(pub, out, tmp_path):
    target = out / "demo.svg"
    run(pub.create(SAVE, str(target)))

    for path in out.iterdir():
        path.unlink()
    out.rmdir()

    result = run(pub.now(save=SAVE))
    assert result["results"][0]["deleted"] is True
    assert pub.list()["count"] == 0
    assert not out.exists()  # never recreated


def test_a_removed_diagram_deletes_the_link(pub, store, out):
    run(pub.create(SAVE, str(out / "demo.svg")))
    store.remove(SAVE, author="tester")

    run(pub.now())
    assert pub.list()["count"] == 0


def test_stop_leaves_the_published_file(pub, out):
    target = out / "demo.svg"
    link = run(pub.create(SAVE, str(target)))

    pub.stop(link["id"])
    assert pub.list()["count"] == 0
    assert target.is_file()


def test_stop_on_an_unknown_link(pub):
    with pytest.raises(AutoPublishError, match="no autopublish link"):
        pub.stop("nope")


# --- persistence and restart -----------------------------------------------

def test_links_survive_a_restart(tmp_path, store, out, render):
    registry = tmp_path / "autopublish.json"
    first = AutoPublisher(store, registry, render=render)
    run(first.create(SAVE, str(out / "demo.svg"), page=None))

    second = AutoPublisher(store, registry, render=render)
    assert second.list()["count"] == 1
    assert second.list()["links"][0]["target"] == str(out / "demo.svg")


def test_reconcile_catches_up_an_edit_made_while_down(tmp_path, store, out, render):
    registry = tmp_path / "autopublish.json"
    target = out / "demo.svg"
    first = AutoPublisher(store, registry, render=render)
    run(first.create(SAVE, str(target)))

    # Service is "down": nothing is attached to the store's commit hook.
    store.on_commit = None
    store.write(SAVE, set_value(TEMPLATE, "a", "while-down"), author="tester")
    render.payload = b"<svg>caught-up</svg>"

    second = AutoPublisher(store, registry, render=render)
    result = run(second.reconcile())

    assert result["published"] == 1
    assert target.read_bytes() == b"<svg>caught-up</svg>"


def test_reconcile_skips_links_that_are_current(tmp_path, store, out, render):
    """An unchanged diagram must not re-render every link on every boot."""
    registry = tmp_path / "autopublish.json"
    run(AutoPublisher(store, registry, render=render).create(
        SAVE, str(out / "demo.svg")))

    second = AutoPublisher(store, registry, render=render)
    result = run(second.reconcile())
    assert result["published"] == 0


def test_reconcile_republishes_a_deleted_target(tmp_path, store, out, render):
    registry = tmp_path / "autopublish.json"
    target = out / "demo.svg"
    run(AutoPublisher(store, registry, render=render).create(SAVE, str(target)))
    target.unlink()

    second = AutoPublisher(store, registry, render=render)
    assert run(second.reconcile())["published"] == 1
    assert target.is_file()


def test_reconcile_drops_links_whose_folder_went_away(tmp_path, store, out, render):
    registry = tmp_path / "autopublish.json"
    run(AutoPublisher(store, registry, render=render).create(
        SAVE, str(out / "demo.svg")))
    for path in out.iterdir():
        path.unlink()
    out.rmdir()

    second = AutoPublisher(store, registry, render=render)
    assert run(second.reconcile())["dropped"] == 1
    assert second.list()["count"] == 0


def test_a_corrupt_registry_does_not_stop_the_service(tmp_path, store, render):
    registry = tmp_path / "autopublish.json"
    registry.write_text("{not json", encoding="utf-8")
    assert AutoPublisher(store, registry, render=render).list()["count"] == 0


def test_registry_is_written_atomically(tmp_path):
    registry = Registry(tmp_path / "autopublish.json")
    registry.links["x"] = Link(id="x", save=SAVE, target="/tmp/x.svg")
    registry.save()

    assert not (tmp_path / "autopublish.json.tmp").exists()
    assert Registry.load(tmp_path / "autopublish.json").links["x"].save == SAVE


class _fast_debounce:
    """Shrink the debounce window so the loop tests take milliseconds."""

    def __enter__(self):
        from awm.drawio import autopublish

        self.module = autopublish
        self.original = autopublish.DEBOUNCE_SECONDS
        autopublish.DEBOUNCE_SECONDS = 0.02
        return self

    def __exit__(self, *exc):
        self.module.DEBOUNCE_SECONDS = self.original
        return False
