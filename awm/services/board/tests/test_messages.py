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
    assert set(first) == {"card_id", "seq", "at", "sender", "origin", "body", "agent", "node"}
    assert first["agent"] is None and first["node"] is None


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
    assert trilium.notes[msgs[0]]["title"] == "1 · john@collins"
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
    gui_board.seal()
    second = gui_board.messages(collins, card["id"])[1]
    assert second["seq"] == 2 and second["body"] == "yes please" and second["origin"] == "gui"
    assert second["sender"] == {"swarm": "tony", "principal": "agent", "party": tony["party_id"]}
    assert trilium.label(note, "msgSeq") == "00000002"
    assert trilium.label(note, "readOnly") == ""
    assert trilium.notes[note]["title"] == "2 · agent (Trilium)"
    assert trilium.label(note, "msgFrom") == "agent (Trilium)"
    assert events.since(0, {"swarm": "collins"})[-1]["card"]["message"]["seq"] == 2
    gui_board.seal()
    assert len(gui_board.messages(collins, card["id"])) == 2


def test_a_note_still_being_typed_or_empty_is_left_alone(gui_board, trilium, tony):
    card = ask(gui_board, tony)
    gui_board.message(tony, card["id"], "first")
    chat = chat_of(trilium, card["id"])
    fresh = typed(trilium, chat, "half a thou")
    trilium.notes[fresh]["modified"] = time.time()
    typed(trilium, chat, "  ")
    gui_board.seal()
    assert len(gui_board.messages(tony, card["id"])) == 1
    trilium.notes[fresh]["modified"] = time.time() - 60
    gui_board.seal()
    assert [m["body"] for m in gui_board.messages(tony, card["id"])] == ["first", "half a thou"]


def test_only_direct_children_of_a_chat_are_sealed(gui_board, trilium, tony):
    card = ask(gui_board, tony)
    gui_board.message(tony, card["id"], "first")
    chat = chat_of(trilium, card["id"])
    (first,) = trilium.children(chat)
    typed(trilium, first, "a note under a message")
    gui_board.seal()
    assert len(gui_board.messages(tony, card["id"])) == 1


def test_without_a_gui_party_notes_stay_unsealed(board, trilium, tony):
    card = ask(board, tony)
    board.message(tony, card["id"], "first")
    note = typed(trilium, chat_of(trilium, card["id"]), "hello")
    board.seal()
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
    assert trilium.notes[note]["title"] == "1 · agent@tony"
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
    assert trilium.notes[note]["title"] == "1 · collins/-/worker one"
    assert trilium.label(note, "msgFrom") == "collins/-/worker one"
    assert trilium.label(note, "msgAgent") == "worker one"
    assert trilium.label(note, "msgNode") is None
    (event,) = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.message"]
    assert event["card"]["message"]["agent"] == "worker one"
    board.sweep()
    assert trilium.notes[note]["title"] == "1 · collins/-/worker one"


@pytest.mark.parametrize("junk", [None, "", "   ", 7, ["a"], {"a": 1}])
def test_a_missing_or_unusable_agent_is_none_and_never_an_error(board, tony, junk):
    card = ask(board, tony)
    assert board.message(tony, card["id"], "hi", agent=junk)["agent"] is None


def test_an_agent_name_is_one_short_line(board, tony):
    card = ask(board, tony)
    sent = board.message(tony, card["id"], "hi", agent="x" * 300 + "\x00\x1b[31m")
    assert len(sent["agent"]) <= 100 and "\x1b" not in sent["agent"]


# -- who wrote a message ---------------------------------------------------------------


def row(origin="api", agent=None, node=None, swarm="tony", principal="agent"):
    return {"card_id": "c", "seq": 3, "at": "t", "origin": origin, "body": "b", "note_id": None,
            "agent": agent, "node": node,
            "sender": {"swarm": swarm, "principal": principal, "party": "p"}}


@pytest.mark.parametrize("message,author", [
    (row(agent="fix agent perms", node="deneb"), "tony/deneb/fix agent perms"),
    (row(agent="fix agent perms"), "tony/-/fix agent perms"),
    (row(agent="a/b", node="deneb"), "tony/deneb/a-b"),
    (row(origin="gui", principal="tony"), "tony (Trilium)"),
    (row(), "agent@tony"),
    (row(node="deneb"), "agent@tony"),
    (row(origin="migrated"), "agent@tony"),
    (row(origin="migrated", agent="old worker", node="mira"), "tony/mira/old worker"),
])
def test_msgfrom_says_who_wrote_the_message_and_the_title_repeats_it(message, author):
    from awm.board.cards import _mirror_fields

    title, labels = _mirror_fields(message)
    assert labels["msgFrom"] == author and title == f"3 · {author}"
    assert labels.get("msgAgent") == message["agent"]
    assert labels.get("msgNode") == message["node"]


def test_a_gui_note_and_an_agent_post_by_the_same_principal_read_differently(
        gui_board, trilium, tony, collins):
    card = ask(gui_board, collins, to="tony")
    gui_board.message(tony, card["id"], "from an agent", agent="fix agent perms", node="deneb")
    note = typed(trilium, chat_of(trilium, card["id"]), "from Tony's hands")
    trilium.notes[note]["modified"] = time.time() - 60
    gui_board.seal()
    notes = {trilium.notes[n]["content"]: n for n in trilium.children(chat_of(trilium, card["id"]))}
    assert trilium.label(notes["from an agent"], "msgFrom") == "tony/deneb/fix agent perms"
    assert trilium.label(notes["from an agent"], "msgNode") == "deneb"
    assert trilium.label(notes["from Tony's hands"], "msgFrom") == "agent (Trilium)"
    assert trilium.label(notes["from Tony's hands"], "msgNode") is None


def test_the_posting_node_is_recorded_and_returned_for_every_way_to_write_a_message(
        board, trilium, events, tony, collins):
    card = ask(board, tony)
    sent = board.message(collins, card["id"], "on it", agent="w", node="deneb")
    assert (sent["agent"], sent["node"]) == ("w", "deneb")
    assert board.messages(tony, card["id"])[0]["node"] == "deneb"
    assert events.messages(card["id"])[0]["node"] == "deneb"
    (event,) = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.message"]
    assert event["card"]["message"]["node"] == "deneb"
    rejected = board.reject(collins, card["id"], "no", agent="w", node="mira")
    assert rejected["message"]["node"] == "mira"
    reply = board.post(collins, kind="message", recipient="tony", title="re", body="reply",
                       reply_to=card["id"], agent="w", node="deneb")
    assert reply["message"]["node"] == "deneb"
    assert [m["node"] for m in board.messages(tony, card["id"])] == ["deneb", "mira", "deneb"]


@pytest.mark.parametrize("junk", [None, "", "../x", "a b", 7, ["a"]])
def test_an_unusable_node_is_none_and_never_an_error(board, tony, junk):
    card = ask(board, tony)
    assert board.message(tony, card["id"], "hi", agent="w", node=junk)["node"] is None


def test_a_card_records_the_node_that_posted_it(board, trilium, tony):
    card = board.post(tony, kind="request", recipient="collins", title="t", body="b",
                      agent="w", node="deneb")
    assert card["node"] == "deneb"
    assert trilium.label(trilium.card_note(card["id"]), "cardNode") == "deneb"
    assert board.get(tony, card["id"])["node"] == "deneb"


def test_the_sweep_rewrites_a_mirror_made_in_the_old_format(board, trilium, tony, collins):
    card = ask(board, tony)
    board.message(collins, card["id"], "on it", agent="worker", node="deneb")
    (note,) = trilium.children(chat_of(trilium, card["id"]))
    trilium.notes[note]["title"] = "1 · collins/john · worker"
    trilium.set_attr(note, "msgFrom", "john@collins")
    trilium.attrs = {k: a for k, a in trilium.attrs.items()
                     if not (a["noteId"] == note and a["name"] == "msgNode")}
    board.sweep()
    assert trilium.notes[note]["title"] == "1 · collins/deneb/worker"
    assert trilium.label(note, "msgFrom") == "collins/deneb/worker"
    assert trilium.label(note, "msgNode") == "deneb"


def test_a_format_change_rewrites_a_bounded_number_of_notes_per_sweep(board, trilium, tony):
    from awm.board.cards import VERIFY_BATCH

    card = ask(board, tony)
    for n in range(VERIFY_BATCH * 2):
        board.message(tony, card["id"], f"m{n}", agent="w", node="deneb")
    chat = chat_of(trilium, card["id"])
    for note in trilium.children(chat):
        trilium.notes[note]["title"] = "old format"
    trilium.calls.clear()
    board.sweep()
    assert len([fn for fn, _ in trilium.calls if fn == "note_update"]) == VERIFY_BATCH
    board.sweep()
    assert not [n for n in trilium.children(chat) if trilium.notes[n]["title"] == "old format"]


def test_a_gui_note_seals_within_the_settle_time_and_not_before(gui_board, trilium, tony):
    from awm.board.cards import SETTLE_S

    card = ask(gui_board, tony)
    gui_board.message(tony, card["id"], "first")
    note = typed(trilium, chat_of(trilium, card["id"]), "hello")
    trilium.notes[note]["modified"] = time.time()  # the fake stamps whole seconds, so age is 0 to 1 s
    gui_board.seal()
    assert len(gui_board.messages(tony, card["id"])) == 1
    trilium.notes[note]["modified"] = time.time() - SETTLE_S - 1.5
    gui_board.seal()
    assert len(gui_board.messages(tony, card["id"])) == 2


def test_the_settle_time_and_seal_tick_keep_a_gui_post_under_about_three_seconds():
    from awm.board import cards, stream

    assert cards.SETTLE_S + stream.SEAL_S <= 2.5
