import os
import subprocess
import sys
import threading

import pytest

from awm.board.cards import COLUMNS, Board, BoardLocked
from awm.board.parties import Conflict, Forbidden, NotFound
from awm.board.vault import VaultError


def request(board, party, to="collins", **kw):
    kw.setdefault("title", "Run the thing")
    kw.setdefault("body", "please")
    return board.post(party, kind="request", recipient=to, **kw)


def message(board, party, to="collins", **kw):
    kw.setdefault("title", "FYI")
    kw.setdefault("body", "hello")
    return board.post(party, kind="message", recipient=to, **kw)


# -- the lifecycle ----------------------------------------------------------------


def test_post_makes_a_card_note_with_the_contract_shape(board, trilium, board_note, tony):
    card = request(board, tony, priority="urgent")
    assert set(card) == {"id", "kind", "sender", "recipient", "claimant", "priority",
                         "status", "title", "body", "reply_to", "result",
                         "created_at", "updated_at"}
    assert card["sender"] == {"swarm": "tony", "principal": "agent", "party": tony["party_id"]}
    assert (card["kind"], card["recipient"], card["priority"]) == ("request", "collins", "urgent")
    assert card["status"] == "posted" and card["claimant"] is None and card["result"] is None
    note_id = trilium.card_note(card["id"])
    assert trilium.notes[note_id]["parentNoteIds"] == [board_note]
    assert trilium.label(note_id, "status") == "Posted"
    assert trilium.label(note_id, "cardTo") == "collins"
    assert trilium.label(note_id, "cardFrom") == "tony"
    assert trilium.label(note_id, "cardParty") == tony["party_id"]
    assert trilium.notes[note_id]["content"] == "please"


def test_the_full_lifecycle_to_done(board, collins, tony):
    card = request(board, tony)
    claimed = board.claim(collins, card["id"])
    assert claimed["status"] == "in_progress" and claimed["claimant"] == "collins"
    done = board.complete(collins, card["id"], "all good")
    assert done["status"] == "done" and done["result"] == "all good"
    assert done["body"] == "please"
    again = board.get(tony, card["id"])
    assert again == done


def test_fail_records_the_reason(board, trilium, collins, tony):
    card = request(board, tony)
    board.claim(collins, card["id"])
    failed = board.fail(collins, card["id"], "no such file")
    assert failed["status"] == "failed" and failed["result"] == "no such file"
    note = trilium.notes[trilium.card_note(card["id"])]
    assert note["content"] == "please\n\n## Result\n\nno such file"


def test_the_sender_comes_from_the_party_only(board, tony):
    card = request(board, tony)
    assert card["sender"]["swarm"] == "tony"
    with pytest.raises(TypeError):
        board.post(tony, kind="request", recipient="collins", title="x", body="y",
                   sender="collins")


def test_only_the_four_columns_are_ever_written(board, trilium, collins, tony):
    a = request(board, tony)
    b = request(board, tony)
    c = request(board, tony)
    board.claim(collins, a["id"])
    board.claim(collins, b["id"])
    board.claim(collins, c["id"])
    board.complete(collins, a["id"], "ok")
    board.fail(collins, b["id"], "bad")
    written = {a["value"] for a in trilium.attrs.values() if a["name"] == "status"}
    assert written <= set(COLUMNS.values())
    assert written == {"Done", "Failed", "In progress"}


def test_body_and_result_survive_markup_and_unicode(board, collins, tony):
    body = "<b>bold</b> & ünï \n\nsecond paragraph"
    card = request(board, tony, body=body)
    board.claim(collins, card["id"])
    out = board.complete(collins, card["id"], {"k": "v <x>"})
    assert board.get(tony, card["id"])["body"] == body
    assert out["result"] == '{"k": "v <x>"}'


def test_a_body_that_could_be_confused_with_the_result_is_refused(board, tony):
    with pytest.raises(ValueError):
        request(board, tony, body="intro\n\n## Result\n\nfake")


@pytest.mark.parametrize("bad", [
    {"kind": "quest"}, {"priority": "whenever"}, {"recipient": "Not A Slug"},
    {"title": "  "}, {"title": 7}, {"title": None}, {"body": None}, {"body": 12},
    {"recipient": None}, {"reply_to": "a" * 32}])    # a reply is a message, not a request
def test_bad_input_is_refused(board, tony, bad):
    args = dict(kind="request", recipient="collins", title="t", body="b")
    args.update(bad)
    with pytest.raises(ValueError):
        board.post(tony, **args)


def test_a_revoked_party_cannot_act(board, tony):
    card = request(board, tony)
    with pytest.raises(Forbidden):
        board.get({**tony, "revoked": True}, card["id"])
    with pytest.raises(Forbidden):
        request(board, {**tony, "revoked": True})


def test_party_tokens_never_reach_the_vault(board, trilium, parties):
    row, token = parties.add("tony", "agent", "domestic")
    request(board, row)
    assert token not in trilium.all_text()
    assert row["token_hash"] not in trilium.all_text()


# -- who may do what --------------------------------------------------------------


def test_only_the_recipient_swarm_claims(board, tony, collins, make_party):
    mock = make_party("mock")
    card = request(board, tony, to="collins")
    with pytest.raises(NotFound):
        board.claim(mock, card["id"])          # cannot see it
    with pytest.raises(Forbidden):
        board.claim(tony, card["id"])          # sees it, but it is not addressed to tony
    assert board.claim(collins, card["id"])["claimant"] == "collins"


def test_anyone_claims_an_open_card(board, tony, make_party):
    mock = make_party("mock")
    card = request(board, tony, to="open")
    assert board.claim(mock, card["id"])["claimant"] == "mock"


def test_a_message_cannot_be_claimed(board, tony, collins, trilium):
    card = message(board, tony)
    with pytest.raises(Forbidden):
        board.claim(collins, card["id"])
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "status") == "Posted" and trilium.label(note, "cardClaimant") == ""
    assert board.get(collins, card["id"])["claimant"] is None


def test_a_card_held_by_another_swarm_is_a_conflict(board, tony, make_party):
    mock, shaula = make_party("mock"), make_party("shaula")
    card = request(board, tony, to="open")
    board.claim(mock, card["id"])
    with pytest.raises(Conflict):
        board.claim(shaula, card["id"])


def test_a_finished_card_cannot_be_claimed_again(board, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    board.complete(collins, card["id"], "ok")
    with pytest.raises(Conflict):
        board.claim(collins, card["id"])


def test_a_repeat_claim_by_the_holder_returns_the_card_without_a_new_event(
        board, events, tony, collins):
    card = request(board, tony)
    first = board.claim(collins, card["id"])
    before = events.latest_id()
    again = board.claim(collins, card["id"])
    assert again == first and again["status"] == "in_progress"
    assert events.latest_id() == before


def test_a_failed_first_claim_leaves_a_card_the_retry_can_take(
        board, trilium, events, tony, collins, make_party):
    card = request(board, tony, to="open")
    trilium.fail_after_labels = 1                  # claimant lands, status does not
    with pytest.raises(VaultError):
        board.claim(collins, card["id"])
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "cardClaimant") == "collins"
    assert trilium.label(note, "status") == "Posted"
    other = make_party("mock")                     # a stale claimant does not hold the card
    retried = board.claim(other, card["id"])
    assert retried["claimant"] == "mock" and retried["status"] == "in_progress"
    assert trilium.label(note, "cardClaimant") == "mock"
    claims = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.claimed"]
    assert [e["card"]["claimant"] for e in claims] == ["mock"]


def test_a_retry_by_the_same_swarm_after_a_failed_claim_works(board, trilium, tony, collins):
    card = request(board, tony)
    trilium.fail_after_labels = 2
    with pytest.raises(VaultError):
        board.claim(collins, card["id"])
    assert board.claim(collins, card["id"])["status"] == "in_progress"


def test_a_timeout_after_the_write_lets_the_holder_retry_and_keeps_others_out(
        board, trilium, events, tony, collins, make_party):
    card = request(board, tony, to="open")
    trilium.fail_after_apply = True                # the claim landed, the reply was lost
    with pytest.raises(VaultError):
        board.claim(collins, card["id"])
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "status") == "In progress"
    with pytest.raises(Conflict):
        board.claim(make_party("mock"), card["id"])
    retried = board.claim(collins, card["id"])
    assert retried["status"] == "in_progress" and retried["claimant"] == "collins"
    claims = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.claimed"]
    assert len(claims) == 1                        # the log gets the claim once, on the retry
    before = events.latest_id()
    board.claim(collins, card["id"])
    assert events.latest_id() == before


def test_a_failed_finish_can_be_retried(board, trilium, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    trilium.fail_after_labels = 1
    with pytest.raises(VaultError):
        board.complete(collins, card["id"], "ok")
    assert board.complete(collins, card["id"], "ok")["result"] == "ok"
    assert board.get(tony, card["id"])["body"] == "please"


def test_only_the_claimant_completes_or_fails(board, tony, collins, make_party):
    mock = make_party("mock")
    card = request(board, tony, to="open")
    board.claim(collins, card["id"])
    for who in (tony, mock):
        with pytest.raises(Forbidden):
            board.complete(who, card["id"], "x")
        with pytest.raises(Forbidden):
            board.fail(who, card["id"], "x")
    assert board.complete(collins, card["id"], "ok")["status"] == "done"


def test_an_unclaimed_card_cannot_be_completed(board, tony, collins):
    card = request(board, tony)
    with pytest.raises(Forbidden):
        board.complete(collins, card["id"], "x")


def test_a_finished_card_cannot_finish_again(board, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    board.complete(collins, card["id"], "ok")
    with pytest.raises(Forbidden):
        board.complete(collins, card["id"], "again")
    with pytest.raises(Forbidden):
        board.fail(collins, card["id"], "again")


def test_a_reply_is_a_new_message_card_pointing_back(board, tony, collins):
    ask = request(board, tony)
    reply = message(board, collins, to="tony", reply_to=ask["id"])
    assert reply["reply_to"] == ask["id"] and reply["kind"] == "message"
    assert reply["claimant"] is None
    assert board.get(tony, reply["id"])["reply_to"] == ask["id"]
    assert [c["id"] for c in board.list(tony, reply_to=ask["id"])] == [reply["id"]]
    assert board.get(tony, ask["id"])["status"] == "posted"


def test_a_reply_to_a_card_the_party_cannot_see_is_not_found(board, tony, collins, make_party):
    mock = make_party("mock")
    ask = request(board, tony, to="collins")
    with pytest.raises(NotFound):
        message(board, mock, to="tony", reply_to=ask["id"])
    with pytest.raises(NotFound):
        message(board, collins, to="tony", reply_to="f" * 32)


# -- visibility and listing -------------------------------------------------------


def test_visibility_filters_get_and_list(board, tony, collins, make_party):
    mock = make_party("mock")
    sent = request(board, tony, to="collins")
    to_tony = request(board, collins, to="tony")
    open_card = request(board, collins, to="open")
    hidden = request(board, collins, to="mock")
    assert {c["id"] for c in board.list(tony)} == {sent["id"], to_tony["id"], open_card["id"]}
    assert {c["id"] for c in board.list(mock)} == {hidden["id"], open_card["id"]}
    for who, secret in ((tony, hidden), (mock, sent)):
        with pytest.raises(NotFound):
            board.get(who, secret["id"])


def test_list_filters(board, tony, collins):
    a = request(board, tony, to="collins")
    b = message(board, tony, to="collins")
    c = request(board, collins, to="tony")
    board.claim(collins, a["id"])
    ids = lambda **f: {x["id"] for x in board.list(collins, **f)}
    assert ids() == {a["id"], b["id"], c["id"]}
    assert ids(sender="tony") == {a["id"], b["id"]}
    assert ids(recipient="tony") == {c["id"]}
    assert ids(kind="message") == {b["id"]}
    assert ids(status="in_progress") == {a["id"]}
    assert ids(claimant="collins") == {a["id"]}
    assert ids(status="posted", kind="request") == {c["id"]}


def test_list_orders_by_creation_and_includes_bodies(board, tony):
    first = request(board, tony, body="one")
    second = request(board, tony, body="two")
    assert [(c["id"], c["body"]) for c in board.list(tony)] == [
        (first["id"], "one"), (second["id"], "two")]


def test_unknown_filters_and_statuses_are_refused(board, tony):
    with pytest.raises(ValueError):
        board.list(tony, colour="red")
    with pytest.raises(ValueError):
        board.list(tony, status="Posted")


# -- atomic claims ----------------------------------------------------------------


def test_a_threaded_double_claim_has_exactly_one_winner(board, trilium, events, tony, make_party):
    claimers = [make_party(name) for name in ("collins", "mock", "shaula", "capella", "deneb")]
    card = request(board, tony, to="open")
    trilium.latency = 0.02   # widen the window between the re-read and the write
    barrier = threading.Barrier(len(claimers))
    outcomes = {}

    def go(party):
        barrier.wait()
        try:
            outcomes[party["swarm"]] = board.claim(party, card["id"])
        except Conflict:
            outcomes[party["swarm"]] = "conflict"

    threads = [threading.Thread(target=go, args=(p,)) for p in claimers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [s for s, r in outcomes.items() if r != "conflict"]
    assert len(winners) == 1 and len(outcomes) == len(claimers)
    assert board.get(tony, card["id"])["claimant"] == winners[0]
    claims = [e for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.claimed"]
    assert len(claims) == 1 and claims[0]["card"]["claimant"] == winners[0]


def test_a_gui_move_before_the_claim_makes_the_claim_a_conflict(board, trilium, tony, collins):
    card = request(board, tony)
    trilium.gui_move(card["id"], "Done")
    with pytest.raises(Conflict):
        board.claim(collins, card["id"])
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "status") == "Done" and trilium.label(note, "cardClaimant") == ""


def test_a_gui_move_between_the_reread_and_the_write_loses_to_the_claim(
        board, trilium, events, tony, collins):
    card = request(board, tony)
    fired = []

    def drag(args):
        if not fired:
            fired.append(1)
            trilium.gui_move(card["id"], "Failed")

    trilium.hooks["note_update"] = [drag]
    claimed = board.claim(collins, card["id"])     # last write wins
    assert fired and claimed["status"] == "in_progress"
    note = trilium.card_note(card["id"])
    assert trilium.label(note, "status") == "In progress"
    assert trilium.label(note, "cardClaimant") == "collins"
    assert board.sweep() == []                     # nothing left for the watcher to report
    kinds = [e["type"] for e in events.since(0, {"swarm": "collins"})]
    assert kinds == ["card.posted", "card.claimed"]


def test_a_claim_and_the_watcher_do_not_interleave(board, trilium, tony, collins):
    card = request(board, tony)
    trilium.latency = 0.01
    stop = threading.Event()
    seen = []

    def watch():
        while not stop.is_set():
            seen.extend(board.sweep())

    t = threading.Thread(target=watch)
    t.start()
    try:
        board.claim(collins, card["id"])
        board.complete(collins, card["id"], "ok")
    finally:
        stop.set()
        t.join()
    assert seen == []     # the board's own writes are never reported as moves


# -- the watcher ------------------------------------------------------------------


def test_a_gui_drag_is_reported_as_card_moved(board, trilium, events, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    trilium.gui_move(card["id"], "Done")
    moved = board.sweep()
    assert len(moved) == 1
    assert moved[0]["type"] == "card.moved"
    assert moved[0]["card"]["id"] == card["id"] and moved[0]["card"]["status"] == "done"
    replay = events.since(0, {"swarm": "tony"})
    assert replay[-1]["type"] == "card.moved" and replay[-1]["id"] == moved[0]["id"]
    assert board.get(tony, card["id"])["status"] == "done"
    assert board.sweep() == []                      # reported once


def test_the_watcher_does_not_report_what_it_has_not_seen_change(board, trilium, tony):
    request(board, tony)
    assert board.sweep() == []
    assert board.sweep() == []


def test_a_drag_back_to_posted_releases_the_claim(board, trilium, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    trilium.gui_move(card["id"], "Posted")
    [event] = board.sweep()
    assert event["card"]["status"] == "posted" and event["card"]["claimant"] is None
    assert board.claim(collins, card["id"])["status"] == "in_progress"


def test_a_drag_that_happened_while_the_board_was_down_is_reported(
        board, trilium, events, tony, collins, vault, tmp_path):
    card = request(board, tony)
    board.close()
    trilium.gui_move(card["id"], "Done")
    with Board(vault, events, lock_path=tmp_path / "board.lock") as restarted:
        assert [e["card"]["id"] for e in restarted.sweep()] == [card["id"]]


def test_a_card_seen_for_the_first_time_is_recorded_without_an_event(
        board, trilium, events, tony, vault, board_note):
    trilium.add_note("by hand", board_note, labels={
        "cardId": "d" * 32, "cardKind": "request", "cardFrom": "tony", "cardTo": "collins",
        "status": "Posted"})
    assert board.sweep() == []
    assert events.seen_all()["d" * 32]["status"] == "posted"
    trilium.gui_move("d" * 32, "Done")
    assert [e["card"]["status"] for e in board.sweep()] == ["done"]


def test_a_deleted_card_is_forgotten(board, trilium, events, tony):
    card = request(board, tony)
    note = trilium.card_note(card["id"])
    del trilium.notes[note]
    assert board.sweep() == []
    assert card["id"] not in events.seen_all()


# -- one board per lock file ------------------------------------------------------


def test_a_respawn_in_the_same_process_reuses_the_lock(board, vault, events, tmp_path):
    again = Board(vault, events, lock_path=tmp_path / "board.lock")
    assert again._guard is board._guard      # one claim lock for every board on the path
    again.close()
    request_ok = board.post({"swarm": "tony", "principal": "a", "party_id": "p"},
                            kind="request", recipient="collins", title="t", body="b")
    assert request_ok["status"] == "posted"


def test_the_flock_is_held_until_the_last_board_in_the_process_closes(
        board, vault, events, tmp_path):
    second = Board(vault, events, lock_path=tmp_path / "board.lock")
    lock = str(tmp_path / "board.lock")
    board.close()
    assert _probe(lock) == 7                 # the respawn still holds it
    second.close()
    assert _probe(lock) != 7


def _probe(lock: str) -> int:
    code = ("import sys\n"
            "from awm.board.cards import Board, BoardLocked\n"
            "try:\n"
            "    Board(None, None, lock_path=sys.argv[1])\n"
            "except BoardLocked:\n"
            "    sys.exit(7)\n")
    return subprocess.run([sys.executable, "-c", code, lock], env=os.environ,
                          capture_output=True).returncode


def test_two_threads_on_two_boards_still_claim_once(
        board, vault, events, trilium, tmp_path, tony, make_party):
    other = Board(vault, events, lock_path=tmp_path / "board.lock")
    card = request(board, tony, to="open")
    trilium.latency = 0.02
    barrier = threading.Barrier(2)
    results = []

    def go(b, who):
        barrier.wait()
        try:
            results.append(b.claim(who, card["id"])["claimant"])
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=go, args=(b, make_party(name)))
               for b, name in ((board, "collins"), (other, "mock"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    other.close()
    assert sorted(results)[0] == "conflict" and len(results) == 2


def test_a_second_process_is_refused_and_a_released_lock_is_free(
        board, vault, events, tmp_path):
    probe = ("import sys\n"
             "from awm.board.cards import Board, BoardLocked\n"
             "try:\n"
             "    Board(None, None, lock_path=sys.argv[1])\n"
             "except BoardLocked:\n"
             "    sys.exit(7)\n")
    lock = str(tmp_path / "board.lock")
    held = subprocess.run([sys.executable, "-c", probe, lock], env=os.environ)
    assert held.returncode == 7
    board.close()
    freed = subprocess.run([sys.executable, "-c", probe, lock], env=os.environ,
                           capture_output=True)
    assert freed.returncode != 7      # it got past the lock (and then tripped on the None vault)
    Board(vault, events, lock_path=lock).close()


# -- reads take no claim lock ----------------------------------------------------


def test_get_and_list_do_not_wait_for_the_claim_lock(board, tony):
    card = request(board, tony)
    held, release, out = threading.Event(), threading.Event(), {}

    def hold():
        with board._guard:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)

    def read():
        out["get"] = board.get(tony, card["id"])
        out["list"] = board.list(tony)

    reader = threading.Thread(target=read)
    reader.start()
    reader.join(2)
    release.set()
    holder.join()
    assert not reader.is_alive() and out["get"]["id"] == card["id"]
    assert [c["id"] for c in out["list"]] == [card["id"]]


def test_list_pages_with_limit_and_offset(board, tony):
    ids = [request(board, tony, body=str(i))["id"] for i in range(5)]
    assert [c["id"] for c in board.list(tony, limit=2)] == ids[:2]
    assert [c["id"] for c in board.list(tony, limit=2, offset=2)] == ids[2:4]
    assert [c["id"] for c in board.list(tony, offset=4)] == ids[4:]
    assert [c["body"] for c in board.list(tony, limit=2, offset=1)] == ["1", "2"]
    assert len(board.list(tony)) == 5


def test_list_reads_bodies_only_for_the_page(board, trilium, tony):
    for i in range(6):
        request(board, tony)
    trilium.calls.clear()
    board.list(tony, limit=2)
    assert len([1 for fn, _ in trilium.calls if fn == "note_get"]) == 2


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_bad_paging_is_refused(board, tony, bad):
    with pytest.raises(ValueError):
        board.list(tony, **bad)


# -- columns the board does not own ------------------------------------------------


def test_columns_match_case_insensitively(board, trilium, tony, collins):
    card = request(board, tony)
    trilium.gui_move(card["id"], "in progress")
    assert board.get(tony, card["id"])["status"] == "in_progress"
    [event] = board.sweep()
    assert event["card"]["status"] == "in_progress"


def test_an_unknown_column_keeps_the_last_known_status(board, trilium, events, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    trilium.gui_move(card["id"], "Blocked")
    assert board.get(tony, card["id"])["status"] == "in_progress"
    assert board.sweep() == []
    assert [c["status"] for c in board.list(tony)] == ["in_progress"]
    assert board.get(tony, card["id"])["status"] != "blocked"
    assert events.seen_all()[card["id"]]["status"] == "in_progress"
    trilium.gui_move(card["id"], "Done")             # back into a known column
    assert [e["card"]["status"] for e in board.sweep()] == ["done"]


def test_an_unknown_column_on_a_card_never_seen_reads_as_posted(board, trilium, board_note):
    trilium.add_note("hand made", board_note, labels={
        "cardId": "e" * 32, "cardFrom": "tony", "cardTo": "collins", "status": "Someday"})
    assert board.get({"swarm": "collins"}, "e" * 32)["status"] == "posted"


# -- the watcher also watches the recipient and the claimant ---------------------------


def test_a_hand_edit_of_the_recipient_is_reported(board, trilium, tony, collins):
    card = request(board, tony, to="collins")
    trilium.set_attr(trilium.card_note(card["id"]), "cardTo", "mock")
    [event] = board.sweep()
    assert event["type"] == "card.moved" and event["card"]["recipient"] == "mock"
    assert board.sweep() == []


def test_a_hand_edit_of_the_claimant_is_reported(board, trilium, tony, collins):
    card = request(board, tony)
    board.claim(collins, card["id"])
    trilium.set_attr(trilium.card_note(card["id"]), "cardClaimant", "mock")
    [event] = board.sweep()
    assert event["card"]["claimant"] == "mock" and event["card"]["status"] == "in_progress"
    assert board.sweep() == []
