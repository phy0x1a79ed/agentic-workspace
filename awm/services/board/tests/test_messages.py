"""A card's thread: the board's record of each message and its read-only Trilium mirror."""

import time

import pytest

from awm.board.parties import Forbidden, NotFound
from awm.board.vault import CHAT_FOLDER, CHAT_MESSAGE


def ask(board, party, to="collins"):
    return board.post(party, kind="request", recipient=to, title="Run the thing", body="please")


def chat_of(trilium, card_id):
    card_note = trilium.card_note(card_id)
    (chat,) = [n for n in trilium.children(card_note)]
    return chat


def test_a_message_gets_the_next_sequence_and_the_poster_from_the_party(board, tony, collins):
    card = ask(board, tony)
    first = board.message(collins, card["id"], "on it")
    second = board.message(tony, card["id"], "thanks")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["sender"] == {"swarm": "collins", "principal": "john",
                               "party": collins["party_id"]}
    assert first["origin"] == "api" and first["at"].endswith("+00:00")
    assert set(first) == {"card_id", "seq", "at", "sender", "origin", "body", "agent"}
    assert first["agent"] is None


def test_messages_read_in_sequence_order_after_a_cursor(board, tony, collins):
    card = ask(board, tony)
    for n in range(4):
        board.message(collins if n % 2 else tony, card["id"], f"m{n}")
    assert [m["body"] for m in board.messages(tony, card["id"])] == ["m0", "m1", "m2", "m3"]
    assert [m["seq"] for m in board.messages(collins, card["id"], after=2)] == [3, 4]
    assert [m["seq"] for m in board.messages(collins, card["id"], after=1, limit=2)] == [2, 3]


def test_a_party_that_cannot_see_the_card_gets_not_found_both_ways(board, tony, make_party):
    mock = make_party("mock")
    card = ask(board, tony)
    with pytest.raises(NotFound):
        board.message(mock, card["id"], "let me in")
    with pytest.raises(NotFound):
        board.messages(mock, card["id"])
    with pytest.raises(NotFound):
        board.message(tony, "f" * 32, "nothing here")


def test_a_revoked_party_and_an_empty_body_are_refused(board, tony):
    card = ask(board, tony)
    with pytest.raises(Forbidden):
        board.message({**tony, "revoked": True}, card["id"], "hi")
    with pytest.raises(ValueError):
        board.message(tony, card["id"], "   ")


def test_each_message_is_a_read_only_note_under_the_cards_chat(board, trilium, tony, collins):
    card = ask(board, tony)
    board.message(collins, card["id"], "on it")
    board.message(tony, card["id"], "thanks")
    chat = chat_of(trilium, card["id"])
    note = trilium.notes[chat]
    assert note["title"] == "Chat" and note["type"] == "book"
    assert trilium.label(chat, "viewType") == "chat"
    assert trilium.label(chat, "sorted") == "msgSeq"
    assert trilium.label(chat, CHAT_FOLDER) == ""
    owned = [a for a in trilium.attrs.values() if a["noteId"] == chat and a["name"] == CHAT_MESSAGE]
    assert owned and owned[0]["isInheritable"]
    msgs = sorted(trilium.children(chat), key=lambda n: trilium.label(n, "msgSeq"))
    assert [trilium.notes[n]["content"] for n in msgs] == ["on it", "thanks"]
    assert trilium.notes[msgs[0]]["title"] == "1 · collins/john"
    assert trilium.label(msgs[0], "msgSeq") == "00000001"
    assert trilium.label(msgs[0], "msgFrom") == "john@collins"
    assert trilium.label(msgs[0], "readOnly") == ""
    assert trilium.label(trilium.card_note(card["id"]), "cardLastMsg") == "2"


def test_messages_never_look_like_cards(board, trilium, tony, collins):
    card = ask(board, tony)
    board.message(collins, card["id"], "on it")
    chat = chat_of(trilium, card["id"])
    for note_id in [chat, *trilium.children(chat)]:
        assert trilium.label(note_id, "cardId") is None
        assert trilium.label(note_id, "status") is None
    assert [c["id"] for c in board.list(tony)] == [card["id"]]
    assert board.sweep() == []


def test_one_chat_note_serves_every_message_even_after_a_restart(board, trilium, vault, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "one")
    vault._chat_by_card.clear()
    board.message(tony, card["id"], "two")
    card_note = trilium.card_note(card["id"])
    assert len(trilium.children(card_note)) == 1


def test_the_card_reports_its_last_message(board, tony, collins):
    card = ask(board, tony)
    board.message(collins, card["id"], "on it")
    assert board.get(tony, card["id"])["last_message"] == 1


def test_each_message_logs_one_card_message_event_with_the_card(board, events, tony, collins):
    card = ask(board, tony)
    board.message(collins, card["id"], "on it")
    (event,) = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.message"]
    assert event["card"]["id"] == card["id"] and event["card"]["recipient"] == "collins"
    assert event["card"]["last_message"] == 1
    assert event["card"]["message"]["body"] == "on it"
    assert event["card"]["message"]["sender"]["swarm"] == "collins"
    assert [e["type"] for e in events.since(0, {"swarm": "collins"})][-1] == "card.message"
    assert not [e for e in events.since(0, {"swarm": "mock"}) if e["type"] == "card.message"]


def test_a_failed_mirror_still_records_the_message(board, trilium, events, tony):
    card = ask(board, tony)
    trilium.hooks["note_create"] = [lambda args: (_ for _ in ()).throw(RuntimeError("down"))]
    sent = board.message(tony, card["id"], "kept")
    assert sent["seq"] == 1
    assert [m["body"] for m in board.messages(tony, card["id"])] == ["kept"]
    assert events.messages(card["id"])[0]["note_id"] is None


# -- notes people add in the Trilium GUI, and notes people edit --------------------


@pytest.fixture
def gui_board(board, tony):
    board.gui_party = tony
    return board


def typed(trilium, chat, text):
    """What adding a child note under the chat in the GUI does."""
    return trilium.add_note("new note", parent=chat, content=text)


def test_a_note_a_person_adds_under_the_chat_becomes_the_next_message(
        gui_board, trilium, events, tony, collins):
    card = ask(gui_board, collins, to="tony")
    gui_board.message(collins, card["id"], "need a hand?")
    chat = chat_of(trilium, card["id"])
    note = typed(trilium, chat, "yes please")
    gui_board.sweep()
    second = gui_board.messages(collins, card["id"])[1]
    assert second["seq"] == 2 and second["body"] == "yes please" and second["origin"] == "gui"
    assert second["sender"] == {"swarm": "tony", "principal": "agent", "party": tony["party_id"]}
    assert trilium.label(note, "msgSeq") == "00000002"
    assert trilium.label(note, "readOnly") == ""
    assert trilium.notes[note]["title"] == "2 · tony/agent"
    assert events.since(0, {"swarm": "collins"})[-1]["card"]["message"]["seq"] == 2
    gui_board.sweep()
    assert len(gui_board.messages(collins, card["id"])) == 2


def test_a_note_still_being_typed_or_empty_is_left_alone(gui_board, trilium, tony):
    card = ask(gui_board, tony)
    gui_board.message(tony, card["id"], "first")
    chat = chat_of(trilium, card["id"])
    fresh = typed(trilium, chat, "half a thou")
    trilium.notes[fresh]["modified"] = time.time()
    typed(trilium, chat, "  ")
    gui_board.sweep()
    assert len(gui_board.messages(tony, card["id"])) == 1
    trilium.notes[fresh]["modified"] = time.time() - 60
    gui_board.sweep()
    assert [m["body"] for m in gui_board.messages(tony, card["id"])] == ["first", "half a thou"]


def test_only_direct_children_of_a_chat_are_sealed(gui_board, trilium, tony):
    card = ask(gui_board, tony)
    gui_board.message(tony, card["id"], "first")
    chat = chat_of(trilium, card["id"])
    (first,) = trilium.children(chat)
    typed(trilium, first, "a note under a message")
    gui_board.sweep()
    assert len(gui_board.messages(tony, card["id"])) == 1


def test_without_a_gui_party_notes_stay_unsealed(board, trilium, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "first")
    note = typed(trilium, chat_of(trilium, card["id"]), "hello")
    board.sweep()
    assert len(board.messages(tony, card["id"])) == 1
    assert trilium.label(note, "msgSeq") is None


def test_an_edited_message_note_changes_nothing_and_is_put_back(board, trilium, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "the original")
    (note,) = trilium.children(chat_of(trilium, card["id"]))
    trilium.notes[note]["content"] = "rewritten history"
    trilium.notes[note]["title"] = "mine now"
    trilium.set_attr(note, "msgFrom", "someone@else")
    assert board.messages(tony, card["id"])[0]["body"] == "the original"
    board.sweep()
    assert trilium.notes[note]["content"] == "the original"
    assert trilium.notes[note]["title"] == "1 · tony/agent"
    assert trilium.label(note, "msgFrom") == "agent@tony"


def test_a_deleted_message_note_is_made_again(board, trilium, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "keep me")
    chat = chat_of(trilium, card["id"])
    (note,) = trilium.children(chat)
    del trilium.notes[note]
    board.sweep()
    (again,) = trilium.children(chat)
    assert again != note and trilium.notes[again]["content"] == "keep me"


def test_an_unmirrored_message_is_mirrored_by_the_sweep(board, trilium, events, tony):
    card = ask(board, tony)
    trilium.hooks["note_create"] = [lambda args: (_ for _ in ()).throw(RuntimeError("down"))]
    board.message(tony, card["id"], "late")
    trilium.hooks.clear()
    board.sweep()
    (note,) = trilium.children(chat_of(trilium, card["id"]))
    assert trilium.notes[note]["content"] == "late"
    assert events.messages(card["id"])[0]["note_id"] == note


def test_checking_untouched_notes_writes_nothing(board, trilium, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "steady")
    board.sweep()
    trilium.calls.clear()
    board.sweep()
    assert not [fn for fn, _ in trilium.calls if fn in ("note_update", "note_create")]


def test_a_message_records_the_agent_that_wrote_it_and_the_mirror_shows_it(board, trilium, events, tony, collins):
    card = ask(board, tony)
    sent = board.message(collins, card["id"], "on it", agent="  worker \n one ")
    assert sent["agent"] == "worker one"
    assert board.messages(tony, card["id"])[0]["agent"] == "worker one"
    (note,) = trilium.children(chat_of(trilium, card["id"]))
    assert trilium.notes[note]["title"] == "1 · collins/john · worker one"
    assert trilium.label(note, "msgAgent") == "worker one"
    (event,) = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.message"]
    assert event["card"]["message"]["agent"] == "worker one"
    board.sweep()
    assert trilium.notes[note]["title"] == "1 · collins/john · worker one"


@pytest.mark.parametrize("junk", [None, "", "   ", 7, ["a"], {"a": 1}])
def test_a_missing_or_unusable_agent_is_none_and_never_an_error(board, tony, junk):
    card = ask(board, tony)
    assert board.message(tony, card["id"], "hi", agent=junk)["agent"] is None


def test_an_agent_name_is_one_short_line(board, tony):
    card = ask(board, tony)
    sent = board.message(tony, card["id"], "hi", agent="x" * 300 + "\x00\x1b[31m")
    assert len(sent["agent"]) <= 100 and "\x1b" not in sent["agent"]
