"""The two one-off migrations: dry runs change nothing, applies are idempotent and keep order."""

import json
from datetime import datetime, timezone

import pytest

from awm.board.migrate import guess_project

WRITES = ("note_create", "note_update", "note_move", "note_place", "attr_set", "attr_delete")


def legacy(trilium, board_note, card_id, column, title="old", created="2026-01-01T00:00:00.000+00:00",
           frm="tony", to="collins", reply_to=None, body="", **labels):
    note = trilium.add_note(title, board_note, content=body, labels={
        "cardId": card_id, "cardKind": labels.pop("cardKind", "request"), "cardFrom": frm,
        "cardTo": to, "cardCreated": created, "cardUpdated": created, "status": column,
        **labels})
    if reply_to:
        trilium.set_attr(note, "replyTo", trilium.card_note(reply_to), type="relation")
    return note


def snapshot(trilium):
    return (json.dumps(trilium.notes, sort_keys=True, default=str),
            json.dumps(trilium.attrs, sort_keys=True, default=str))


# -- columns ------------------------------------------------------------------------


@pytest.fixture
def old_board(trilium, board_note):
    legacy(trilium, board_note, "a" * 32, "Posted")
    legacy(trilium, board_note, "b" * 32, "In progress", cardClaimant="collins")
    recent = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    legacy(trilium, board_note, "c" * 32, "Done", created=recent)
    legacy(trilium, board_note, "d" * 32, "Failed", created=recent)
    return trilium


def test_the_columns_dry_run_reports_the_plan_and_changes_nothing(board, old_board, events):
    before = snapshot(old_board)
    calls = len(old_board.calls)
    report = board.migrate_columns(dry_run=True)
    assert report["dry_run"] is True and report["cards_to_move"] == 4
    assert report["by_state"] == {"proposed": 1, "planned": 1, "done": 1, "rejected": 1}
    assert report["folders_to_create"] == ["proposed", "planned", "active", "blocked", "review",
                                           "done", "rejected"]
    assert snapshot(old_board) == before
    assert not [fn for fn, _ in old_board.calls[calls:] if fn in WRITES]
    assert events.latest_id() == 0


def test_the_columns_apply_files_every_card_by_its_status_and_removes_the_grouping(
        board, old_board, board_note, events, tony):
    report = board.migrate_columns(dry_run=False)
    assert report["moved"] == 4
    assert [old_board.state_of(c * 32) for c in "abcd"] == ["proposed", "planned", "done", "rejected"]
    assert old_board.columns() == ["proposed", "planned", "active", "blocked", "review", "done", "rejected"]
    assert not [a for a in old_board.attrs.values() if a["name"] == "status"]
    assert old_board.label(board_note, "board:columnsFrom") == "folders"
    assert events.latest_id() == 0                          # filing a card is not a state change
    assert board.sweep() == []
    again = board.migrate_columns(dry_run=False)
    assert again["moved"] == 0 and again["cards_to_move"] == 0 and again["folders_to_create"] == []
    states = {c["id"]: c["state"] for c in board.list({"swarm": "collins"})}
    assert states == {"a" * 32: "proposed", "b" * 32: "planned", "c" * 32: "done", "d" * 32: "rejected"}


def test_a_second_columns_apply_makes_no_write(board, old_board):
    board.migrate_columns(dry_run=False)
    calls = len(old_board.calls)
    board.migrate_columns(dry_run=False)
    assert not [fn for fn, _ in old_board.calls[calls:] if fn in WRITES]


# -- threads ------------------------------------------------------------------------


@pytest.fixture
def chains(trilium, board_note, vault):
    """Root A with a reply chain, root B alone, root C a test, and an unrelated card on A's topic."""
    vault.ensure_columns()
    ids = {k: str(i) * 32 for i, k in enumerate("abcdefgh", start=1)}

    def card(key, column, **kw):
        return legacy(trilium, board_note, ids[key], column, **kw)

    card("a", "Posted", title="Factorio blueprint request", created="2026-01-01T00:00:00.000+00:00",
         body="please send the blueprint")
    card("b", "Posted", title="Re: Factorio blueprint request", created="2026-01-03T00:00:00.000+00:00",
         frm="collins", to="tony", reply_to=ids["a"], body="here it is", cardKind="message")
    card("c", "Posted", title="Re: Re: blueprint", created="2026-01-02T00:00:00.000+00:00",
         reply_to=ids["a"], body="which one?", cardKind="message", frm="collins", to="tony")
    card("d", "Posted", title="Re: Re: Re:", created="2026-01-04T00:00:00.000+00:00",
         reply_to=ids["b"], body="", cardKind="message")
    card("e", "Posted", title="smoke test ping", created="2026-01-05T00:00:00.000+00:00")
    card("f", "Posted", title="Another Factorio request", created="2026-01-06T00:00:00.000+00:00")
    card("g", "In progress", title="Sync federation board", created="2026-01-07T00:00:00.000+00:00")
    card("h", "Posted", title="Lunch", created="2026-01-08T00:00:00.000+00:00")
    return ids


def test_the_heuristic_names_factorio_federation_tests_or_nothing():
    assert guess_project("smoke test", "") == "tests"
    assert guess_project("Build", "uses factorio mods") == "factorio"
    assert guess_project("Sync", "the federated board") == "federation"
    assert guess_project("Lunch", "run the tests tomorrow") is None
    assert guess_project("test factorio", "") == "tests"


def test_the_threads_dry_run_proposes_and_changes_nothing(board, trilium, chains, tmp_path):
    before = snapshot(trilium)
    path = tmp_path / "proposal.json"
    summary = board.migrate_threads(dry_run=True, proposal_path=path)
    assert snapshot(trilium) == before
    assert summary["cards_folded"] == 3 and summary["chains_with_replies"] == 1
    assert summary["chains"] == 5                        # a, e, f, g, h
    assert summary["projects"] == {"factorio": 2, "federation": 1, "none": 1, "tests": 1}
    assert summary["to_done"] == 1
    proposal = json.loads(path.read_text())
    by_root = {s["root"]: s for s in proposal["subjects"]}
    a = by_root[chains["a"]]
    assert [r["id"] for r in a["replies"]] == [chains["c"], chains["b"], chains["d"]]   # created_at order
    assert a["project"] == "factorio" and a["to_done"] is False
    assert by_root[chains["e"]]["to_done"] is True and by_root[chains["e"]]["project"] == "tests"
    assert chains["b"] not in by_root                    # replies are not subjects


def test_a_reply_posted_on_this_release_is_left_out_of_the_plan(board, trilium, board_note,
                                                                chains, tmp_path):
    legacy(trilium, board_note, "9" * 32, "Posted", reply_to=chains["a"], cardKind="message",
           frm="collins", to="tony", cardThreaded=chains["a"])
    path = tmp_path / "proposal.json"
    summary = board.migrate_threads(dry_run=True, proposal_path=path)
    assert summary["cards_folded"] == 3 and summary["chains"] == 5
    ids = {r["id"] for s in json.loads(path.read_text())["subjects"] for r in s["replies"]}
    assert "9" * 32 not in ids


def test_two_chains_that_share_a_topic_are_not_merged(board, chains, tmp_path):
    path = tmp_path / "p.json"
    board.migrate_threads(dry_run=True, proposal_path=path)
    by_root = {s["root"]: s for s in json.loads(path.read_text())["subjects"]}
    assert by_root[chains["f"]]["replies"] == [] and by_root[chains["a"]]["project"] == "factorio"
    assert chains["f"] in by_root and chains["a"] in by_root


def apply(board, tmp_path, edit=None):
    path = tmp_path / "proposal.json"
    board.migrate_threads(dry_run=True, proposal_path=path)
    if edit:
        proposal = json.loads(path.read_text())
        edit(proposal)
        path.write_text(json.dumps(proposal))
    return board.migrate_threads(dry_run=False, proposal_path=path)


def test_apply_backfills_the_thread_in_original_order_and_folds_the_cards(
        board, trilium, chains, events, tmp_path):
    report = apply(board, tmp_path)
    assert report["messages_backfilled"] == 3 and report["replies_archived"] == 3
    root = {"swarm": "tony"}
    thread = board.messages(root, chains["a"])
    assert [(m["seq"], m["body"]) for m in thread] == [
        (1, "which one?"), (2, "here it is"), (3, "Re: Re: Re:")]   # the empty body keeps its title
    assert [m["at"] for m in thread] == ["2026-01-02T00:00:00.000+00:00",
                                        "2026-01-03T00:00:00.000+00:00",
                                        "2026-01-04T00:00:00.000+00:00"]
    assert thread[0]["sender"]["swarm"] == "collins" and thread[0]["origin"] == "migrated"
    cards = {c["id"] for c in board.list(root)}
    assert not cards & {chains["b"], chains["c"], chains["d"]}          # archived, hidden
    assert board.get(root, chains["b"])["id"] == chains["b"]            # but never deleted
    assert trilium.label(trilium.card_note(chains["b"]), "archived") == ""
    assert trilium.is_under(chains["b"], chains["a"]) and board.get(root, chains["b"])["state"] == "proposed"
    assert not trilium.is_filed_archived(chains["b"])                   # filed under its home, no Archived folder
    assert not [c for fn, c in trilium.calls if fn == "note_place"
                and set(c["parents"]) & set(trilium.archives_of_all())]
    assert board.get(root, chains["a"])["last_message"] == 3
    assert board.get(root, chains["a"])["project"] == "factorio"
    mirrored = sorted(trilium.children(trilium.chat_of(chains["a"])), key=lambda n: trilium.label(n, "msgSeq"))
    assert [trilium.notes[n]["content"] for n in mirrored] == [
        "which one?", "here it is", "Re: Re: Re:"]
    assert [trilium.label(n, "msgSeq") for n in mirrored] == ["00000001", "00000002", "00000003"]
    assert not [e for e in events.since(0, root) if e["type"] == "card.message"]    # silent
    assert board.sweep() == []


def test_apply_sets_projects_and_moves_test_subjects_to_done(board, trilium, chains, events, tmp_path):
    report = apply(board, tmp_path)
    assert report["moved_to_done"] == 1 and report["projects_set"] == 4
    assert trilium.state_of(chains["e"]) == "done"
    assert trilium.label(trilium.card_note(chains["e"]), "project") == "tests"
    assert trilium.label(trilium.card_note(chains["g"]), "project") == "federation"
    assert trilium.label(trilium.card_note(chains["h"]), "project") is None
    assert board.get({"swarm": "tony"}, chains["f"])["state"] == "proposed"
    assert [e["card"]["state"] for e in events.since(0, {"swarm": "tony"}) if e["type"] == "card.moved"] == ["done"]


def test_a_second_apply_changes_nothing(board, trilium, chains, events, tmp_path):
    apply(board, tmp_path)
    path = tmp_path / "proposal.json"
    before, latest = snapshot(trilium), events.latest_id()
    msgs = events.messages(chains["a"])
    again = board.migrate_threads(dry_run=False, proposal_path=path)
    assert snapshot(trilium) == before and events.latest_id() == latest
    assert events.messages(chains["a"]) == msgs
    assert again.get("messages_backfilled", 0) == 0 and again.get("replies_archived", 0) == 0
    assert again.get("projects_set", 0) == 0 and again.get("moved_to_done", 0) == 0
    # a fresh dry run sees nothing left to fold
    assert board.migrate_threads(dry_run=True, proposal_path=tmp_path / "q.json")["cards_folded"] == 0


def test_backfill_sorts_before_messages_the_root_already_has(board, trilium, chains, events, tmp_path):
    tony, collins = {"swarm": "tony", "principal": "agent", "party": "p1"}, \
        {"swarm": "collins", "principal": "john", "party": "p2"}
    board.message(tony | {"party_id": "p1"}, chains["a"], "live one")
    board.message(collins | {"party_id": "p2"}, chains["a"], "live two")
    assert [m["seq"] for m in events.messages(chains["a"])] == [1, 2]
    apply(board, tmp_path)
    thread = board.messages({"swarm": "tony"}, chains["a"])
    assert [(m["seq"], m["body"]) for m in thread] == [
        (1, "which one?"), (2, "here it is"), (3, "Re: Re: Re:"), (4, "live one"), (5, "live two")]
    assert board.get({"swarm": "tony"}, chains["a"])["last_message"] == 5
    chat = trilium.chat_of(chains["a"])
    titles = {trilium.label(n, "msgSeq"): trilium.notes[n]["title"] for n in trilium.children(chat)}
    assert sorted(titles) == ["0000000%d" % i for i in range(1, 6)]
    assert titles["00000004"].startswith("4 ·") and titles["00000005"].startswith("5 ·")
    contents = [trilium.notes[n]["content"] for n in sorted(
        trilium.children(chat), key=lambda n: trilium.label(n, "msgSeq"))]
    assert contents[-2:] == ["live one", "live two"]
    assert board.sweep() == []
    trilium.calls.clear()
    board.sweep()
    assert not [fn for fn, _ in trilium.calls if fn in ("note_update", "note_create")]


def test_a_reply_whose_chain_no_longer_leads_to_the_root_is_left_alone(board, trilium, chains, tmp_path):
    def forge(proposal):
        subject = next(s for s in proposal["subjects"] if s["root"] == chains["f"])
        subject["replies"] = [{"id": chains["h"], "created_at": "x", "from": "tony", "title": "x"}]

    report = apply(board, tmp_path, forge)
    assert report["replies_left_alone"] == 1
    assert board.get({"swarm": "tony"}, chains["h"])["id"] == chains["h"]
    assert board.messages({"swarm": "tony"}, chains["f"]) == []


def test_an_edited_proposal_decides_the_project_and_a_bad_one_is_refused(board, trilium, chains, tmp_path):
    def edit(proposal):
        next(s for s in proposal["subjects"] if s["root"] == chains["h"])["project"] = "lunch"
        next(s for s in proposal["subjects"] if s["root"] == chains["e"])["to_done"] = False

    apply(board, tmp_path, edit)
    assert trilium.label(trilium.card_note(chains["h"]), "project") == "lunch"
    assert board.get({"swarm": "tony"}, chains["e"])["state"] == "proposed"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "subjects": [
        {"root": chains["h"], "project": "Not A Slug", "replies": []}]}))
    with pytest.raises(ValueError):
        board.migrate_threads(dry_run=False, proposal_path=path)
    with pytest.raises(ValueError):
        board.migrate_threads(dry_run=False, proposal_path=None)
    with pytest.raises(ValueError):
        board.migrate_threads(dry_run=False, proposal_path=tmp_path / "missing.json")


def test_a_reply_cycle_is_skipped_rather_than_looped(board, trilium, board_note, vault, tmp_path):
    vault.ensure_columns()
    one = legacy(trilium, board_note, "1" * 32, "Posted", cardKind="message")
    legacy(trilium, board_note, "2" * 32, "Posted", cardKind="message", reply_to="1" * 32)
    trilium.set_attr(one, "replyTo", trilium.card_note("2" * 32), type="relation")
    summary = board.migrate_threads(dry_run=True, proposal_path=tmp_path / "p.json")
    assert summary["cyclic_skipped"] >= 1 and summary["cards_folded"] == 0
