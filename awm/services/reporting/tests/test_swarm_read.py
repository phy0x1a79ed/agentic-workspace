"""Reading and writing Swarm-board cards from a session on any node of the swarm."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from awm.reporting import hub_adapter
from awm.reporting.swarmboard import BoardError, NotACard, SwarmBoard, plain

from stubs import FakeFed, FakeTrilium, make_board, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

CARD_HTML = ("<ul>\n<li><strong>State:</strong> Done</li>\n<li><strong>Peer:</strong> deneb</li>\n</ul>\n"
             "<hr>\n<details><summary>Plan</summary>\n<p>(none)</p>\n</details>\n")


@pytest.fixture
def tri():
    return FakeTrilium()


@pytest.fixture
def board(tri):
    ids = make_board(tri)
    card = tri.add("fxdiff-renders", ids["Done"], content=CARD_HTML,
                   labels={"agentManaged": "", "cardStateAt": "2026-10-26T18:36:04.867+00:00",
                           "cardFrom": "deneb", "project": "relay", "swarmTags": "lab"})
    tri.add("Plan", card, type="code", mime="text/x-markdown", content="# Plan\n\n1. render",
            labels={"agentManaged": ""})
    active = tri.add("factorio-diffusion-2", ids["Active"], content="<p>working</p>",
                     labels={"cardStateAt": "2026-10-26T09:00:00.000+00:00", "owner": "tony/deneb/fx"})
    old = tri.add("old work", ids["Older Done"], labels={"cardStateAt": "2026-09-01T00:00:00.000+00:00"})
    return SimpleNamespace(tri=tri, ids=ids, card=card, active=active, old=old, board=SwarmBoard(tri))


def test_plain_reads_a_card_body_as_text():
    assert plain(CARD_HTML, "text/html") == "- State: Done\n- Peer: deneb\n\nPlan\n\n(none)"
    assert plain("# keep *this*", "text/x-markdown") == "# keep *this*"


async def test_cards_lists_each_column_with_the_chip_labels(board):
    cards = await board.board.cards()
    assert [(c["card_id"], c["column"]) for c in cards] == [(board.active, "Active"), (board.card, "Done")]
    done = cards[1]
    assert done["from"] == "deneb" and done["project"] == "relay" and done["tags"] == "lab"
    assert done["state_at"].startswith("2026-10-26T18")


async def test_cards_takes_a_column_by_state_and_lists_older_only_when_asked(board):
    assert [c["card_id"] for c in await board.board.cards("done")] == [board.card]
    older = await board.board.cards("Done", older=True)
    assert [(c["card_id"], c.get("older")) for c in older] == [(board.card, None), (board.old, True)]
    with pytest.raises(BoardError, match="no column"):
        await board.board.cards("doing")


async def test_card_reads_its_text_and_its_children(board):
    card = await board.board.card(board.card)
    assert card["column"] == "Done" and card["title"] == "fxdiff-renders"
    assert card["body"].startswith("- State: Done")
    assert card["children"] == [{"note_id": card["children"][0]["note_id"], "title": "Plan",
                                 "text": "# Plan\n\n1. render"}]
    assert card["messages"] == 0 and card["files"] == []
    assert (await board.board.card(board.old))["older"] is True


async def test_a_note_off_the_board_is_not_a_card(board):
    with pytest.raises(NotACard, match="not a card"):
        await board.board.card(board.ids["Ledger"])
    with pytest.raises(NotACard, match="no Trilium note"):
        await board.board.card("tFvgsByaL6sH")


async def test_a_posted_message_reads_back_in_order_and_stays_off_the_children(board):
    first = await board.board.post_message(board.card, "fxdiff-design on deneb", "first")
    await board.board.post_message(board.card, "Opal on altair", "second")
    note = board.tri.notes[first["message_id"]]
    assert note["parents"] == [board.card] and note["mime"] == "text/x-markdown"
    assert {"agentManaged", "readOnly", "swarmMessage"} <= set(note["labels"])
    msgs = await board.board.messages(board.card)
    assert [(m["seq"], m["from"], m["body"]) for m in msgs] == [
        (1, "fxdiff-design on deneb", "first\n"), (2, "Opal on altair", "second\n")]
    assert [m["seq"] for m in await board.board.messages(board.card, after=1)] == [2]
    card = await board.board.card(board.card)
    assert card["messages"] == 2 and [c["title"] for c in card["children"]] == ["Plan"]


async def test_a_message_needs_a_body_and_a_card(board):
    with pytest.raises(BoardError, match="empty"):
        await board.board.post_message(board.card, "x", "  ")
    with pytest.raises(NotACard):
        await board.board.post_message(board.ids["Done"], "x", "hi")


async def test_a_file_lists_and_reads_back(board):
    att = board.tri.attach(board.card, "render.png", b"\x89PNG", mime="image/png")
    assert await board.board.files(board.card) == [
        {"file_id": att, "name": "render.png", "mime": "image/png", "size": 4}]
    meta, blob = await board.board.file(board.card, att)
    assert meta["name"] == "render.png" and blob == b"\x89PNG"
    with pytest.raises(BoardError, match="no file"):
        await board.board.file(board.active, att)


# -- the operations verbs ------------------------------------------------------


@pytest.fixture
def runtime(monkeypatch, board):
    fed = FakeFed()
    rt = SimpleNamespace(board=board.board, fed=fed)
    monkeypatch.setattr(hub_adapter, "RUNTIME", rt)
    return SimpleNamespace(rt=rt, fed=fed, board=board)


async def test_get_takes_a_note_id_or_a_trilium_link(runtime):
    card = runtime.board.card
    out = await hub_adapter.HANDLERS["swarm_get"]({"card_id": card}, None)
    assert out["ok"] is True and out["card"]["column"] == "Done" and "federation_card" not in out["card"]
    link = f"https://nexus.example/trilium/#root/abc/{card}"
    assert (await hub_adapter.HANDLERS["swarm_get"]({"card_id": link}, None))["card"]["card_id"] == card


async def test_get_names_the_federation_card_a_claimed_note_belongs_to(runtime):
    fed_card = runtime.fed.add(make_card(note_id=runtime.board.active))
    out = await hub_adapter.HANDLERS["swarm_get"]({"card_id": runtime.board.active}, None)
    assert out["card"]["federation_card"] == fed_card["id"]
    msgs = await hub_adapter.HANDLERS["swarm_messages"]({"card_id": runtime.board.active}, None)
    assert msgs["federation_card"] == fed_card["id"] and "board_messages" in msgs["notice"]


async def test_a_federation_id_is_pointed_at_the_board_verbs(runtime):
    fed_card = runtime.fed.add(make_card())
    out = await hub_adapter.HANDLERS["swarm_get"]({"card_id": fed_card["id"][:8]}, None)
    assert out["ok"] is False and "board_get" in out["error"] and fed_card["id"] in out["error"]
    out = await hub_adapter.HANDLERS["swarm_get"]({"card_id": "nosuchnote99"}, None)
    assert out["ok"] is False and "no Trilium note" in out["error"]


async def test_the_board_answers_this_node_and_domestic_peers_only(runtime, monkeypatch):
    monkeypatch.setattr(hub_adapter, "peer_relation", {"deneb": "domestic", "caviar": "foreign"}.get)
    verb = hub_adapter.HANDLERS["swarm_list"]
    assert (await verb({}, "peer:deneb"))["ok"] is True
    for caller in ("peer:caviar", "peer:stranger", "user:guest"):
        out = await verb({}, caller)
        assert out["ok"] is False and "domestic" in out["error"]


async def test_post_signs_with_the_session_and_node(runtime, monkeypatch):
    from awm.board import agent

    monkeypatch.setattr(agent, "session_name", lambda pid: "fxdiff-design" if pid == 42 else None)
    monkeypatch.setattr("awm.config.node_name", lambda: "deneb")
    out = await hub_adapter.HANDLERS["swarm_message_post"](
        {"card_id": runtime.board.card, "body": "renders look right", "_caller_pid": 42}, None)
    assert out["ok"] is True and out["message"]["from"] == "fxdiff-design on deneb"
    monkeypatch.setattr(hub_adapter, "peer_relation", lambda name: "domestic")
    out = await hub_adapter.HANDLERS["swarm_message_post"](
        {"card_id": runtime.board.card, "body": "from afar"}, "peer:capella")
    assert out["message"]["from"] == "a session on capella"


async def test_file_fetch_lands_read_only_in_quarantine(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("AWM_BOARD_QUARANTINE", str(tmp_path / "q"))
    card = runtime.board.card
    att = runtime.board.tri.attach(card, "../render.png", b"\x89PNG", mime="image/png")
    fetch = hub_adapter.HANDLERS["swarm_file_fetch"]
    out = await fetch({"card_id": card, "file_id": att}, None)
    path = out["path"]
    assert out["ok"] is True and path.startswith(str(tmp_path / "q" / "swarm" / card / att))
    assert open(path, "rb").read() == b"\x89PNG" and not os.access(path, os.W_OK)
    assert (await fetch({"card_id": card, "file_id": att}, None))["path"] == path
    monkeypatch.setattr(hub_adapter, "peer_relation", lambda name: "domestic")
    assert (await fetch({"card_id": card, "file_id": att}, "peer:capella"))["ok"] is False
