"""The board writer and board-install against a local Trilium (tests/trilium-up.sh)."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from awm.taskqueue import board
from awm.taskqueue.board import ROOT_ID, card_note_id, record_note_id
from awm.taskqueue.etapi import Etapi
from awm.taskqueue.store import QueueError, Store
from awm.taskqueue.writer import Writer

pytestmark = pytest.mark.trilium
SECRET = b"s" * 32
HERE = Path(__file__).parent


@pytest.fixture(scope="session")
def trilium() -> tuple[str, str]:
    """``AWM_TEST_TRILIUM="URL TOKEN"`` reuses a running Trilium. Otherwise start the container."""
    env = os.environ.get("AWM_TEST_TRILIUM")
    if env:
        url, token = env.split()
        return url, token
    if not shutil.which("docker"):
        pytest.skip("no docker for the Trilium container")
    out = subprocess.run([str(HERE / "trilium-up.sh"), "18080"], capture_output=True, text=True,
                         timeout=300)
    if out.returncode != 0 or len(out.stdout.split()) != 2:
        pytest.skip(f"Trilium did not start: {out.stderr[-300:]}")
    url, token = out.stdout.split()
    return url, token


@pytest.fixture
def api(trilium):
    a = Etapi(*trilium)
    yield a
    a.close()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "queue.db", action_secret=SECRET)


def labels(api: Etapi, note_id: str) -> dict[str, str]:
    return {a["name"]: a["value"] for a in api.note(note_id)["attributes"] if a["type"] == "label"}


def act(store: Store, action: str, task_id, args: dict) -> dict:
    ts = store.clock()
    return store.card_action(action, task_id, args, ts, store.action_mac(action, task_id, args, ts))


def test_writer_mirrors_cards(store, api):
    w = Writer(store, api)
    t = store.task_open("Fix the tunnel", "cc-1", peer="deneb", name="fix-tunnel",
                        rc_url="https://claude.ai/code/session_x")["task_id"]
    assert w.step() > 0
    nid = card_note_id(t)
    assert api.note(nid)["parentNoteIds"] == [ROOT_ID]
    got = labels(api, nid)
    assert got["taskId"] == t and got["section"] == "working" and got["peer"] == "deneb"
    assert "status" not in got and "doneAt" not in got and "swarmCard" in got
    assert '"rc_url": "https://claude.ai/code/session_x"' in api.content(record_note_id(t))
    assert "queueHeartbeat" in labels(api, ROOT_ID)

    store.task_report(t, "cc-1", "blocked", "needs a key")
    w.step()
    got = labels(api, nid)
    assert got["section"] == "needs_you" and got["status"] == "blocked"
    store.task_report(t, "cc-1", "done")
    w.step()
    got = labels(api, nid)
    assert got["section"] == "done" and int(got["doneAt"]) > 0
    assert w.step() == 0  # the mark moved past every event
    assert not api.search("#swarmLedger") and not api.search("note.title = Ledger")


def test_proposed_body_is_tonys_and_spawn_checks_it(store, api):
    w = Writer(store, api)
    store.body_reader = w.read_body
    t = act(store, "note_new", None, {"title": "Rework the board"})["task_id"]
    w.step()
    nid = card_note_id(t)
    assert store.task_get(t)["body_ref"] == nid
    assert api.note(nid)["mime"] == "text/x-markdown"
    body = "# Rework\n\nFour sections, no drag.\n"
    api.set_content(nid, body)  # Tony writes the note in Trilium

    w.upsert(store.task_get(t))  # a later upsert leaves the body alone
    assert api.content(nid) == body

    with pytest.raises(QueueError, match="changed"):
        act(store, "spawn", t, {"peer": "deneb", "body_sha256": hashlib.sha256(b"old").hexdigest()})
    out = act(store, "spawn", t, {"peer": "deneb",
                                  "body_sha256": hashlib.sha256(body.encode()).hexdigest()})
    assert out["body"] == body and out["body_ref"] == nid
    w.step()
    assert labels(api, nid)["section"] == "working" and api.content(nid) == body


def test_fold_removes_the_joiners_card(store, api):
    w = Writer(store, api)
    a = store.task_open("Main task", "cc-a")["task_id"]
    b = store.task_open("Joiner's own", "cc-b")["task_id"]
    w.step()
    assert api.note(card_note_id(b)) is not None
    store.task_join(a, "cc-b")
    w.step()
    assert api.note(card_note_id(b)) is None
    assert '"cc-b"' in api.content(record_note_id(a))


def test_done_pages_back_on_done_at(store, api):
    base = 1_900_000_000 + secrets.randbelow(10**6) * 100  # the Trilium outlives each run
    clock = [float(base)]
    store.clock = lambda: clock[0]
    w = Writer(store, api, clock=lambda: clock[0])
    ids = []
    for i in range(3):
        clock[0] += 10
        t = store.task_open(f"done {i}", f"cc-d{i}")["task_id"]
        store.task_report(t, f"cc-d{i}", "done")
        ids.append(t)
    w.step()
    q = "#swarmCard #section=done #doneAt > " + str(base) + " #doneAt < {} orderBy #doneAt desc limit 2"
    first = [n["noteId"] for n in api.search(q.format(base + 31))]
    assert first == [card_note_id(ids[2]), card_note_id(ids[1])]
    cursor = labels(api, first[-1])["doneAt"]
    assert [n["noteId"] for n in api.search(q.format(cursor))] == [card_note_id(ids[0])]


def test_board_install_is_idempotent(api):
    out = board.install(api, "https://approve.test/")
    again = board.install(api, "https://approve.test")
    assert out == again
    root = api.note(ROOT_ID)
    attrs = {(a["type"], a["name"]): a["value"] for a in root["attributes"]}
    assert attrs[("relation", "renderNote")] == board.PAGE_ID
    assert attrs[("label", "approverOrigin")] == "https://approve.test"
    assert root["type"] == "render"
    assert api.note(board.SCRIPT_ID)["mime"] == "application/javascript;env=frontend"
    assert "awm-panel" in api.content(board.PAGE_ID)
    assert sum(1 for a in root["attributes"] if a["name"] == "renderNote") == 1
