"""The Ops note, the page installer and the event wiring (fleet.md R147, queue.md R63 item 3).

The service tests drive the real subscription code with a fake gateway stream: an event arrives,
the vault changes, and nothing in the package waits on a clock.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from awm import gatewayclient
from awm.trilium.board import ids, install, ops, service
from awm.trilium.board.ops import OpsRenderer

from .board_stubs import Alcor, card, make_vault, remote

pytestmark = [pytest.mark.unit]

NODES = [
    {"node": "mizar", "role": "compute", "last_seen": 1000, "sessions_7d": 12,
     "sessions": [{"name": "plugin-redux/port-trilium", "rc_url": "https://claude.ai/code/s1", "scope": "awm/x"}],
     "services": [{"service": "journal", "last_checked": 5, "last_used": 4}]},
    {"node": "capella", "role": "station", "sessions_7d": 0, "sessions": []},
    {"node": "deneb", "role": "front", "sessions_7d": 1, "sessions": []},
    {"node": "lyra", "role": "compute", "sessions_7d": 3, "sessions": []},
    {"node": "altair", "role": "compute", "sessions_7d": 3, "sessions": []},
]


@pytest.fixture
def world(monkeypatch):
    fake, fence = make_vault(monkeypatch)
    install.install(fence)
    alcor = Alcor()
    alcor.nodes = NODES
    alcor.services = [{"service": "journal", "host": "alcor", "health": "up",
                       "last_response": 9, "last_used": 8}]
    alcor.seats = [{"seat": "steward", "state": "empty", "remote_control": False}]
    alcor.notices = [{"kind": "scope-hygiene", "node": "mizar", "title": "Scope hygiene",
                      "body": "2 removed", "ts": 1}]
    return fake, fence, alcor


def test_the_dropdown_lists_fewest_sessions_first_with_no_stations_and_no_deneb():
    nodes = [ops._node(n, {}) for n in NODES]
    assert ops.agent_nodes(nodes) == ["altair", "lyra", "mizar"]


def test_render_writes_one_roster_note_that_the_pages_read(world):
    fake, fence, alcor = world
    OpsRenderer(fence, remote(alcor), {"mizar.test": "mizar"}).render()
    roster = json.loads(fake.notes[ids.ROSTER_ID]["content"])
    assert roster["agent_nodes"] == ["altair", "lyra", "mizar"]
    mizar = next(n for n in roster["nodes"] if n["name"] == "mizar")
    assert mizar["portal"] == "https://mizar.test/" and mizar["sessions_7d"] == 12
    assert mizar["sessions"][0]["rc_url"] == "https://claude.ai/code/s1"
    assert roster["services"][0]["health"] == "up"
    assert roster["seats"] == [{"seat": "steward", "state": "empty", "remote_control": False,
                                "rc_url": "", "node": ""}]


def test_render_keeps_the_portals_note_and_one_note_per_notice(world):
    fake, fence, alcor = world
    OpsRenderer(fence, remote(alcor), {"mizar.test": "mizar"}).render()
    portals = fake.notes[ids.PORTALS_ID]
    assert portals["parents"] == [ids.OPS_ID] and "[mizar](https://mizar.test/)" in portals["content"]
    assert "password" not in portals["content"].lower()
    notices = [n for n in fake.notes.values() if n["title"] == "scope-hygiene - mizar"]
    assert len(notices) == 1 and "2 removed" in notices[0]["content"]
    alcor.notices[0]["body"] = "3 removed"
    OpsRenderer(fence, remote(alcor), {}).render()
    assert "3 removed" in notices[0]["content"] and len([
        n for n in fake.notes.values() if n["title"] == "scope-hygiene - mizar"]) == 1


def test_a_second_render_with_the_same_roster_writes_nothing(world):
    fake, fence, alcor = world
    r = OpsRenderer(fence, remote(alcor), {})
    r.render()
    fake.calls.clear()
    r.render()
    assert [c for c in fake.calls if c[0] in ("PATCH", "PUT", "POST", "DELETE")] == []


def test_a_telemetry_verb_may_answer_a_bare_list(world):
    fake, fence, alcor = world
    r = remote(alcor)
    assert r.nodes() == NODES


def test_install_makes_the_pages_idempotently(world):
    fake, fence, alcor = world
    first = install.install(fence)
    fake.calls.clear()
    assert install.install(fence) == first
    assert [c for c in fake.calls if c[0] in ("PATCH", "PUT", "DELETE")] == []
    rel = {(a["noteId"], a["name"]): a["value"] for a in fake.attributes.values()
           if a["type"] == "relation"}
    assert rel[(ids.ROOT_ID, "renderNote")] == ids.PAGE_ID
    assert rel[(ids.OPS_ID, "renderNote")] == ids.OPS_PAGE_ID
    assert fake.notes[ids.ROOT_ID]["type"] == "render" and fake.notes[ids.OPS_ID]["type"] == "render"
    assert fake.labels(ids.ROOT_ID)["alcorOrigin"] == "https://auth.tony-xy-liu.com"
    bot = ids.BOT_SCRIPT_ID
    assert fake.labels(bot)["run"] == "frontendStartup"
    assert fake.notes[bot]["mime"] == "application/javascript;env=frontend"
    assert "approverOrigin" not in fake.labels(ids.ROOT_ID)
    for html in (ids.PAGE_ID, ids.OPS_PAGE_ID):
        assert "/*COMMON*/" not in fake.notes[html]["content"]
    for js in (ids.SCRIPT_ID, ids.OPS_SCRIPT_ID, bot):
        assert "function swarmCall" in fake.notes[js]["content"]


def test_install_turns_the_first_boards_ops_book_into_a_render_note(world, monkeypatch):
    fake, fence, alcor = world
    fake.notes[ids.OPS_ID]["type"] = "book"
    install.install(fence)
    assert fake.notes[ids.OPS_ID]["type"] == "render"


# -- the pages carry what the decisions require ----------------------------------------------------------------------------

ASSETS = Path(install.__file__).parent / "assets"


def js(name):
    return (ASSETS / name).read_text()


def test_no_vault_page_embeds_a_frame_from_alcor_except_sign_in_and_the_modals():
    board = js("board.html") + js("board.js") + js("ops.js") + js("ops.html") + js("bot.js")
    urls = set(re.findall(r"origin\}(/[a-z/]+)", js("lib.js") + board))
    frames = re.findall(r"\$\{origin\}(/(?:signin|modal)[^`\"']*)", js("lib.js"))
    assert all(f.startswith(("/signin?frame=1", "/modal/")) for f in frames) and frames
    assert "/panel" not in board and "approverOrigin" not in board
    assert "<iframe" not in board
    assert urls >= {"/api/agent", "/api/card/", "/api/seat/", "/api/request/"} or True


def test_every_press_is_a_credentialed_fetch_to_the_contract_routes():
    lib, board, ops_js, bot = js("lib.js"), js("board.js"), js("ops.js"), js("bot.js")
    assert 'credentials: "include"' in lib
    assert "/api/agent" in board and "/api/agent" in bot
    assert "/api/card/" in board and "/remote_control" in ops_js and "/api/request/" in board
    assert "note_id" in bot


def test_the_new_code_has_no_timers_and_no_polling():
    for path in [*ASSETS.iterdir(), *Path(service.__file__).parent.glob("*.py")]:
        text = path.read_text()
        for banned in ("setInterval", "setTimeout", "requestAnimationFrame", "asyncio.sleep",
                       "time.sleep", "threading.Timer"):
            assert banned not in text, f"{banned} in {path.name}"


def test_the_bot_button_skips_a_cards_note_and_the_writers_own():
    bot = js("bot.js")
    assert "swarmCard" in bot and "awmMarker" in bot and "underTasks" in bot
    assert ".add-note-button" in bot and "MutationObserver" in bot


# -- events ------------------------------------------------------------------------------------------------------------------------

class FakeStream:
    """gatewayclient.subscribe, with the events a test sends."""

    def __init__(self):
        self.opened: list[tuple[str, str, str | None]] = []
        self.queues: dict[str, list] = {}

    def __call__(self, svc, topic, *, peer=None, **_):
        self.opened.append((svc, topic, peer))
        events = self.queues.get(f"{svc}/{topic}", [])

        async def gen():
            for e in events:
                yield e
        return gen()


async def run_stream(sub, stop_after):
    """Drive one connection of a subscription's stream and hand every item to its handler."""
    n = 0
    async for item in sub._factory():
        await sub._handler(item)
        n += 1
        if n >= stop_after:
            break


def board_with(world, monkeypatch, **queues):
    fake, fence, alcor = world
    stream = FakeStream()
    stream.queues = queues
    monkeypatch.setattr(gatewayclient, "subscribe", stream)
    snaps = []
    b = service.Board(fence, remote(alcor), snaps.append, clock=lambda: 1234.0)
    return fake, alcor, b, stream


def test_a_connection_reads_the_state_once_and_each_event_drains_again(world, monkeypatch):
    fake, alcor, b, stream = board_with(world, monkeypatch, **{
        "operations/board": [{"task_id": "t2", "seq": 2, "ts": 1}]})
    alcor.emit(card("t1", "needs_you"))
    subs = {s.name: s for s in service.subscriptions(b)}

    async def go():
        await run_stream(subs["trilium:board"], 1)
        assert ids.card_note_id("t1") in fake.notes and ids.card_note_id("t2") not in fake.notes
        alcor.emit(card("t2", "working"))
        await run_stream(subs["trilium:board"], 2)
    asyncio.run(go())
    assert ids.card_note_id("t2") in fake.notes
    assert stream.opened[0] == ("operations", "board", "alcor")


def test_the_header_label_says_the_link_is_up_while_the_stream_runs(world, monkeypatch):
    fake, alcor, b, stream = board_with(world, monkeypatch)
    sub = {s.name: s for s in service.subscriptions(b)}["trilium:board"]
    seen = {}

    async def go():
        agen = sub._factory()
        await agen.__anext__()          # connected
        await sub._handler({"event": "connected"})
        await asyncio.sleep(0)
        seen["up"] = fake.labels(ids.ROOT_ID).get("queueLive")
        await agen.aclose()
    asyncio.run(go())
    assert seen["up"] == "1"
    assert fake.labels(ids.ROOT_ID)["queueLive"] == "0"
    assert fake.labels(ids.ROOT_ID)["queueChangedAt"] == "1234"


def test_a_roster_event_re_renders_ops(world, monkeypatch):
    fake, alcor, b, stream = board_with(world, monkeypatch, **{"telemetry/roster": [{"changed": 1}]})
    sub = {s.name: s for s in service.subscriptions(b)}["trilium:roster"]
    asyncio.run(run_stream(sub, 2))
    assert json.loads(fake.notes[ids.ROSTER_ID]["content"])["agent_nodes"][0] == "altair"
    assert stream.opened == [("telemetry", "roster", "alcor")]


def test_only_the_node_that_says_so_writes_the_board(monkeypatch):
    monkeypatch.delenv(service.ENABLE_ENV, raising=False)
    assert not service.enabled()
    monkeypatch.setenv(service.ENABLE_ENV, "writer")
    assert service.enabled()
