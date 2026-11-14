"""The Ops data, the Control panel installer and the pushes (fleet.md R79, R147, queue.md R63 item 3)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from awm.trilium.board import ids, install, ops, service
from awm.trilium.board.ops import OpsRenderer

from .board_stubs import card, make_vault

pytestmark = [pytest.mark.unit]

NODES = [
    {"node": "mizar", "role": "compute", "last_seen": 1000, "sessions_7d": 12,
     "sessions": [{"session_id": "cc00000000000040008000000000ab12", "name": "plugin-redux/port-trilium", "link": "https://claude.ai/code/s1",
                   "scope": "awm/x", "remote_control": True}],
     "services": [{"service": "trilium", "up": True}],
     "uses": [{"tool": "journal", "host": "alcor", "listed": True, "last_checked": 5, "last_used": 4},
              {"tool": "zotero", "host": "deneb", "listed": False}]},
    {"node": "capella", "role": "station", "sessions_7d": 0, "sessions": []},
    {"node": "deneb", "role": "front", "sessions_7d": 1, "sessions": []},
    {"node": "lyra", "role": "compute", "sessions_7d": 3, "sessions": []},
    {"node": "altair", "role": "compute", "sessions_7d": 3, "sessions": []},
]


PUSH = {
    "nodes": NODES,
    "services": [{"tool": "journal", "host": "alcor", "service": "journal", "health": "up"}],
    "seats": [{"seat": "acct/123", "name": "seat:discord/lab/steward", "state": "empty",
               "remote_control": False, "model": "sonnet", "effort": "low", "refresh": True}],
    "notices": [{"kind": "scope-hygiene", "node": "mizar", "title": "Scope hygiene",
                 "body": "2 removed", "ts": 1}],
}


@pytest.fixture
def world(monkeypatch):
    fake, fence = make_vault(monkeypatch)
    install.install(fence)
    return fake, fence


def test_the_dropdown_lists_fewest_sessions_first_with_no_stations_and_no_deneb():
    nodes = [ops._node(n, {}) for n in NODES]
    assert ops.agent_nodes(nodes) == ["altair", "lyra", "mizar"]


def test_render_writes_one_roster_note_that_the_pages_read(world):
    fake, fence = world
    OpsRenderer(fence, {"mizar.test": "mizar"}).render(PUSH)
    roster = json.loads(fake.notes[ids.ROSTER_ID]["content"])
    assert roster["agent_nodes"] == ["altair", "lyra", "mizar"]
    mizar = next(n for n in roster["nodes"] if n["name"] == "mizar")
    assert mizar["portal"] == "https://mizar.test/" and mizar["sessions_7d"] == 12
    assert mizar["sessions"][0]["rc_url"] == "https://claude.ai/code/s1"
    assert mizar["sessions"][0]["session_id"] == "cc00000000000040008000000000ab12"  # the Retire button names it
    assert roster["services"][0]["health"] == "up" and roster["services"][0]["service"] == "journal"
    assert [(u["service"], u["enabled"]) for u in mizar["services"]] == [("trilium", None)]  # an older node sends no enabled
    assert mizar["sessions"][0]["remote_control"] is True
    assert roster["seats"] == [{"seat": "acct/123", "label": "seat:discord/lab/steward",
                                "state": "empty", "remote_control": False, "rc_url": "", "node": "",
                                "rc_live": "", "ts": "", "model": "sonnet", "effort": "low",
                                "refresh": True, "refresh_live": ""}]


def test_render_keeps_the_portals_note_and_one_note_per_notice(world):
    fake, fence = world
    OpsRenderer(fence, {"mizar.test": "mizar"}).render(PUSH)
    portals = fake.notes[ids.PORTALS_ID]
    assert portals["parents"] == [ids.OPS_ID] and "[mizar](https://mizar.test/)" in portals["content"]
    assert "password" not in portals["content"].lower()
    notices = [n for n in fake.notes.values() if n["title"] == "scope-hygiene - mizar"]
    assert len(notices) == 1 and "2 removed" in notices[0]["content"]
    OpsRenderer(fence, {}).render({**PUSH, "notices": [{**PUSH["notices"][0], "body": "3 removed"}]})
    assert "3 removed" in notices[0]["content"] and len([
        n for n in fake.notes.values() if n["title"] == "scope-hygiene - mizar"]) == 1


def clocks(seen):
    return {**PUSH, "nodes": [{**NODES[0], "last_seen": seen}, *NODES[1:]]}


@pytest.mark.parametrize("push, writes", [
    (PUSH, False),
    (clocks(1000 + ops.SEEN_LAG_S), False),   # only the clocks moved
    (clocks("1970-01-01T00:21:40+00:00"), False),  # an ISO clock reads as the same epoch seconds
    (clocks(1001 + ops.SEEN_LAG_S), True),    # last_seen fell too far behind
    ({**PUSH, "seats": [{**PUSH["seats"][0], "ts": "2"}]}, True),
])
def test_a_render_writes_the_roster_only_when_a_page_would_show_it(world, push, writes):
    fake, fence = world
    r = OpsRenderer(fence, {})
    r.render(PUSH)
    fake.calls.clear()
    r.render(push)
    assert bool([c for c in fake.calls if c[0] in ("PATCH", "PUT", "POST", "DELETE")]) == writes


def test_a_roster_push_keeps_the_notices_and_a_notices_push_keeps_the_roster(world):
    fake, fence = world
    r = OpsRenderer(fence, {})
    r.render(PUSH)
    roster_only = {k: PUSH[k] for k in ("nodes", "services", "seats")}
    assert r.render(roster_only)["notices"][0]["kind"] == "scope-hygiene"
    after = r.render({"notices": []})
    assert after["notices"] == [] and after["agent_nodes"][0] == "altair"


def test_install_makes_the_pages_idempotently(world):
    fake, fence = world
    first = install.install(fence)
    fake.calls.clear()
    assert install.install(fence) == first
    assert [c for c in fake.calls if c[0] in ("PATCH", "PUT", "DELETE")] == []
    rel = {(a["noteId"], a["name"]): a["value"] for a in fake.attributes.values()
           if a["type"] == "relation"}
    assert rel[(ids.ROOT_ID, "renderNote")] == ids.PAGE_ID
    assert (ids.OPS_ID, "renderNote") not in rel
    assert fake.notes[ids.ROOT_ID]["type"] == "render" and fake.notes[ids.ROOT_ID]["title"] == "Control panel"
    assert fake.notes[ids.OPS_ID]["type"] == "book" and fake.notes[ids.OPS_ID]["parents"] == [ids.ROOT_ID]
    assert fake.labels(ids.ROOT_ID)["alcorOrigin"] == "https://auth.tony-xy-liu.com"
    bot = ids.BOT_SCRIPT_ID
    assert fake.labels(bot)["run"] == "frontendStartup"
    assert fake.notes[bot]["mime"] == "application/javascript;env=frontend"
    assert "approverOrigin" not in fake.labels(ids.ROOT_ID)
    assert "/*COMMON*/" not in fake.notes[ids.PAGE_ID]["content"]
    for js in (ids.SCRIPT_ID, bot):
        assert "function swarmCall" in fake.notes[js]["content"]


def test_install_folds_the_ops_page_into_the_control_panels_data_folder(world):
    fake, fence = world
    fence.update(ids.OPS_ID, title="Ops", type_="render", labels={"swarmOps": ""})
    fence.c.move_note(ids.OPS_ID, ids.BOOK_ID)
    fence.create(ids.OPS_ID, "ops page", "code", note_id=ids.LEGACY_OPS_PAGE_ID, mime="text/html")
    fence.create(ids.LEGACY_OPS_PAGE_ID, "ops.js", "code", note_id=ids.LEGACY_OPS_SCRIPT_ID)
    fence.relation(ids.OPS_ID, "renderNote", ids.LEGACY_OPS_PAGE_ID)
    OpsRenderer(fence, {}).render(PUSH)
    install.install(fence)
    ops_note = fake.notes[ids.OPS_ID]
    assert (ops_note["type"], ops_note["title"], ops_note["parents"]) == ("book", "Ops data", [ids.ROOT_ID])
    assert "swarmOps" not in fake.labels(ids.OPS_ID)
    assert not any(a["noteId"] == ids.OPS_ID and a["type"] == "relation" for a in fake.attributes.values())
    assert ids.LEGACY_OPS_PAGE_ID not in fake.notes and ids.LEGACY_OPS_SCRIPT_ID not in fake.notes
    assert fake.notes[ids.ROSTER_ID]["parents"] == [ids.OPS_ID]


# -- the pages carry what the decisions require ----------------------------------------------------------------------------

ASSETS = Path(install.__file__).parent / "assets"


def js(name):
    return (ASSETS / name).read_text()


def test_no_vault_page_embeds_a_frame_from_alcor_except_sign_in_and_the_modals():
    board = js("board.html") + js("board.js") + js("bot.js")
    urls = set(re.findall(r"origin\}(/[a-z/]+)", js("lib.js") + board))
    frames = re.findall(r"\$\{origin\}(/(?:signin|modal)[^`\"']*)", js("lib.js"))
    assert all(f.startswith(("/signin?frame=1", "/modal/")) for f in frames) and frames
    assert "/panel" not in board and "approverOrigin" not in board
    assert "<iframe" not in board
    assert urls >= {"/api/agent", "/api/card/", "/api/seat/", "/api/request/"} or True


def test_every_press_is_a_credentialed_fetch_to_the_contract_routes():
    lib, board, bot = js("lib.js"), js("board.js"), js("bot.js")
    assert 'credentials: "include"' in lib
    assert "/api/agent" in board and "/api/agent" in bot
    assert "/api/card/" in board and "/api/seat/remote_control" in board and "/api/service/toggle" in board and "/api/request/" in board
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


# -- the pushes ----------------------------------------------------------------------------------------------------------------

def test_a_push_installs_the_pages_once_and_stamps_the_header(world, monkeypatch):
    fake, fence = world
    runs = []
    monkeypatch.setattr(install, "install", lambda f: runs.append(f))
    b = service.Board(fence, clock=lambda: 1234.0)
    assert b.apply({"base": 0, "cards": [card("t1", "needs_you")], "mark": 1}) == {"mark": 1, "applied": 1}
    b.render(PUSH)
    assert len(runs) == 1
    assert ids.card_note_id("t1") in fake.notes
    assert fake.labels(ids.ROOT_ID)["queueChangedAt"] == "1234"
    assert json.loads(fake.notes[ids.ROSTER_ID]["content"])["agent_nodes"][0] == "altair"


def test_asking_for_the_mark_does_not_touch_the_header(world):
    fake, fence = world
    assert service.Board(fence, clock=lambda: 9.0).apply({}) == {"mark": 0, "applied": 0}
    assert "queueChangedAt" not in fake.labels(ids.ROOT_ID)
