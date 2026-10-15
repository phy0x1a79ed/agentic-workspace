"""The seven states: who may move a card, rejection, history, archiving and the owner labels."""

import pytest

from awm.board.parties import Conflict, Forbidden, NotFound


def request(board, party, to="collins", **kw):
    kw.setdefault("title", "Run the thing")
    kw.setdefault("body", "please")
    return board.post(party, kind="request", recipient=to, **kw)


# -- board_status ------------------------------------------------------------------


@pytest.mark.parametrize("state", ["planned", "active", "blocked", "review", "done"])
def test_the_claimant_moves_its_card_to_every_settable_state(board, trilium, events, tony, collins, state):
    card = request(board, tony)
    board.claim(collins, card["id"])
    board.set_status(collins, card["id"], "blocked")
    moved = board.set_status(collins, card["id"], state)
    assert moved["state"] == state and trilium.state_of(card["id"]) == state
    assert moved["status"] == ("done" if state == "done" else "in_progress")
    last = events.since(0, {"swarm": "tony"})[-1]
    assert last["type"] == "card.moved" and last["card"]["state"] == state
    assert board.sweep() == []                        # the board's own move is not reported again


def test_a_move_to_the_state_it_is_in_logs_nothing(board, events, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    before = events.latest_id()
    assert board.set_status(collins, card["id"], "planned")["state"] == "planned"
    assert events.latest_id() == before


def test_only_the_claimants_swarm_moves_a_card(board, tony, collins, make_party):
    mock = make_party("mock")
    card = request(board, tony, to="open")
    with pytest.raises(Forbidden):
        board.set_status(collins, card["id"], "active")          # nobody holds it yet
    board.claim(collins, card["id"])
    for who in (tony, mock):
        with pytest.raises((Forbidden, NotFound)):
            board.set_status(who, card["id"], "active")


@pytest.mark.parametrize("state", ["proposed", "rejected", "someday", None, "posted"])
def test_board_status_refuses_states_it_does_not_set(board, tony, collins, state):
    card = request(board, tony)
    board.claim(collins, card["id"])
    with pytest.raises(ValueError):
        board.set_status(collins, card["id"], state)


def test_a_finished_card_cannot_be_moved_again(board, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    board.complete(collins, card["id"], "ok")
    with pytest.raises(Forbidden):
        board.set_status(collins, card["id"], "active")


# -- board_reject -------------------------------------------------------------------


def test_the_recipient_rejects_and_the_reason_joins_the_thread(board, trilium, events, tony, collins):
    card = request(board, tony)
    out = board.reject(collins, card["id"], "out of scope", agent="triage")
    assert out["state"] == "rejected" and out["status"] == "failed"
    assert trilium.state_of(card["id"]) == "rejected"
    (msg,) = board.messages(tony, card["id"])
    assert msg["body"] == "out of scope" and msg["sender"]["swarm"] == "collins"
    assert msg["agent"] == "triage" and out["message"]["seq"] == 1
    kinds = [e["type"] for e in events.since(0, {"swarm": "tony"})]
    assert kinds[-2:] == ["card.moved", "card.message"]
    assert [e["card"]["state"] for e in events.since(0, {"swarm": "tony"})
            if e["type"] == "card.moved"] == ["rejected"]


def test_the_claimant_and_the_admin_may_reject_and_nobody_else(board, tony, collins, make_party):
    mock = make_party("mock")
    board.gui_party = tony                              # tony's swarm is the host's admin
    held = request(board, mock, to="open")
    board.claim(collins, held["id"])
    assert board.reject(collins, held["id"], "cannot do it")["state"] == "rejected"
    other = request(board, collins, to="mock")
    with pytest.raises(NotFound):
        board.reject(tony, other["id"], "x")             # the admin sees only what it may see
    seen = request(board, collins, to="open")
    with pytest.raises(Forbidden):
        board.reject(make_party("shaula"), seen["id"], "mine now")
    assert board.reject(tony, seen["id"], "admin says no")["state"] == "rejected"


def test_the_sender_cannot_reject_its_own_card(board, tony):
    card = request(board, tony)
    with pytest.raises(Forbidden):
        board.reject(tony, card["id"], "changed my mind")


def test_a_finished_card_cannot_be_rejected_and_a_reason_is_required(board, tony, collins):
    card = request(board, tony)
    with pytest.raises(ValueError):
        board.reject(collins, card["id"], "  ")
    board.reject(collins, card["id"], "no")
    with pytest.raises(Conflict):
        board.reject(collins, card["id"], "no again")


# -- board_events -------------------------------------------------------------------


def test_events_for_a_card_replay_what_the_party_may_see_after_a_cursor(board, tony, collins, make_party):
    card = request(board, tony)
    other = request(board, tony)
    board.claim(collins, card["id"])
    board.set_status(collins, card["id"], "active")
    board.message(tony, other["id"], "elsewhere")
    got = board.card_events(tony, card["id"])
    assert [e["type"] for e in got] == ["card.posted", "card.claimed", "card.moved"]
    assert got[-1]["card"]["state"] == "active"
    assert [e["type"] for e in board.card_events(collins, card["id"], since=got[0]["id"])] == [
        "card.claimed", "card.moved"]
    assert len(board.card_events(tony, card["id"], limit=1)) == 1
    with pytest.raises(NotFound):
        board.card_events(make_party("mock"), card["id"])
    with pytest.raises(ValueError):
        board.card_events(tony, card["id"], since=-1)


# -- archive ------------------------------------------------------------------------


@pytest.mark.parametrize("finish", ["complete", "fail"])
def test_a_finished_card_is_archived_fourteen_days_after_it_entered_its_state(
        board, trilium, tony, collins, finish):
    from datetime import datetime, timedelta, timezone

    card = request(board, tony)
    board.claim(collins, card["id"])
    getattr(board, finish)(collins, card["id"], "r")
    note = trilium.card_note(card["id"])

    def stamp(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")

    trilium.set_attr(note, "cardStateAt", stamp(13))
    board.sweep()
    assert trilium.label(note, "archived") is None
    trilium.set_attr(note, "cardStateAt", stamp(15))
    board.sweep()
    assert trilium.label(note, "archived") == ""
    assert board.list(tony) == []                         # hidden from the board
    assert board.get(tony, card["id"])["id"] == card["id"]  # still readable by id
    assert board.sweep() == []


def test_open_cards_are_never_archived_and_the_age_counts_from_entering_the_state(
        board, trilium, tony, collins):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="milliseconds")
    card = request(board, tony)
    trilium.set_attr(trilium.card_note(card["id"]), "cardStateAt", old)
    board.sweep()
    assert trilium.label(trilium.card_note(card["id"]), "archived") is None
    board.claim(collins, card["id"])
    board.complete(collins, card["id"], "ok")              # entered Done just now
    board.sweep()
    assert trilium.label(trilium.card_note(card["id"]), "archived") is None


# -- agent, owner and project labels ---------------------------------------------------


def test_a_post_records_the_project_and_agent_and_a_claim_records_the_owner(board, trilium, tony, collins):
    card = request(board, tony, project="factorio", agent="planner")
    note = trilium.card_note(card["id"])
    assert (card["project"], card["agent"]) == ("factorio", "planner")
    assert trilium.label(note, "project") == "factorio" and trilium.label(note, "cardAgent") == "planner"
    assert trilium.label(note, "cardFrom") == "tony" and trilium.label(note, "cardTo") == "collins"
    assert trilium.label(note, "owner") is None
    claimed = board.claim(collins, card["id"], agent="builder", node="mira")
    assert claimed["owner"] == "collins/mira/builder"
    assert trilium.label(note, "owner") == "collins/mira/builder"
    assert board.get(tony, card["id"])["owner"] == "collins/mira/builder"
    assert [c["id"] for c in board.list(tony, project="factorio")] == [card["id"]]
    assert board.list(tony, project="tests") == []


@pytest.mark.parametrize("agent,node,owner", [
    (None, None, "collins"), ("builder", None, "collins/-/builder"),
    (None, "mira", "collins/mira"), ("a/b", "mira", "collins/mira/a-b"), ("x", "bad node!", "collins/-/x")])
def test_the_owner_label_degrades_when_the_agent_or_node_is_unknown(
        board, tony, collins, agent, node, owner):
    card = request(board, tony)
    assert board.claim(collins, card["id"], agent=agent, node=node)["owner"] == owner


@pytest.mark.parametrize("bad", ["Has Caps", "a b", "../x", "x" * 80, 5])
def test_a_project_must_be_a_slug(board, tony, bad):
    with pytest.raises(ValueError):
        request(board, tony, project=bad)


def test_labels_come_from_the_party_and_not_from_the_card_text(board, trilium, tony):
    card = request(board, tony, title="project: tests", body="owner: root\ncardFrom: collins")
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "project") is None and trilium.label(note, "owner") is None
    assert trilium.label(note, "cardFrom") == "tony"
