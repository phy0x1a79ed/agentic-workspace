"""The board writer, board-install and ops_post through the trilium tool (fleet.md R105).

``FakeTrilium`` answers the tool's verbs in memory, ``note_create`` with its optional
``note_id`` included.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from awm.operations import board
from awm.operations.board import ROOT_ID, card_note_id, record_note_id
from awm.operations.relay import Refused
from awm.operations.store import QueueError, Store
from awm.operations.trilium import TriliumTool
from awm.operations.writer import Writer


class FakeTrilium:
    def __init__(self) -> None:
        self.notes: dict[str, dict[str, Any]] = {"root": {"title": "root", "parents": [],
                                                          "labels": {}, "relations": {}}}
        self.calls: list[str] = []
        self._n = 0

    def call_sync(self, verb: str, args: dict[str, Any], **_: Any) -> Any:
        self.calls.append(verb)
        return getattr(self, verb)(**args)

    def _get(self, note_id: str) -> dict[str, Any]:
        if note_id not in self.notes:
            raise Refused(f"404 note {note_id} not found")
        return self.notes[note_id]

    def note_get(self, note_id: str, content: bool = True) -> dict[str, Any]:
        n = self._get(note_id)
        attrs = [{"type": "label", "name": k, "value": v, "noteId": note_id}
                 for k, v in n["labels"].items()]
        attrs += [{"type": "relation", "name": k, "value": v, "noteId": note_id}
                  for k, v in n["relations"].items()]
        out = {"noteId": note_id, "title": n["title"], "type": n.get("type"), "mime": n.get("mime"),
               "parentNoteIds": n["parents"], "attributes": attrs}
        if content:
            out["content"] = n.get("content", "")
        return out

    def note_create(self, title: str, parent: str = "root", note_id: str | None = None,
                    content: str = "", type: str = "text", mime: str | None = None,
                    labels: dict | None = None) -> dict[str, Any]:
        if note_id is None:
            self._n += 1
            note_id = f"auto{self._n}"
        assert note_id not in self.notes
        self.notes[note_id] = {"title": title, "parents": [parent], "content": content,
                               "type": type, "mime": mime, "labels": dict(labels or {}),
                               "relations": {}}
        return {"note_id": note_id, "created": True}

    def note_update(self, note_id: str, title: str | None = None, content: str | None = None,
                    mime: str | None = None, labels: dict | None = None) -> dict[str, Any]:
        n = self._get(note_id)
        for k, v in (("title", title), ("content", content), ("mime", mime)):
            if v is not None:
                n[k] = v
        n["labels"].update(labels or {})
        return {"note_id": note_id}

    def attr_delete(self, note_id: str, name: str, type: str = "label") -> dict[str, Any]:
        self._get(note_id)["labels" if type == "label" else "relations"].pop(name, None)
        return {}

    def attr_set(self, note_id: str, name: str, value: str = "", type: str = "label") -> dict:
        self._get(note_id)["labels" if type == "label" else "relations"][name] = value
        return {}

    def note_delete(self, note_id: str) -> dict[str, Any]:
        self._get(note_id)
        del self.notes[note_id]
        return {"deleted": True}

    def note_search(self, query: str, limit: int = 50) -> dict[str, Any]:
        assert query.startswith("#")
        name = query[1:]
        return {"results": [{"noteId": k} for k, n in self.notes.items() if name in n["labels"]]}

    def note_children(self, note_id: str) -> dict[str, Any]:
        return {"children": [{"note_id": k, "title": n["title"], "labels": sorted(n["labels"])}
                             for k, n in self.notes.items() if note_id in n["parents"]]}


@pytest.fixture
def fake():
    return FakeTrilium()


@pytest.fixture
def api(fake):
    return TriliumTool(tool=fake)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "queue.db")


def labels(fake: FakeTrilium, note_id: str) -> dict[str, str]:
    return dict(fake.notes[note_id]["labels"])


def test_writer_mirrors_cards(store, api, fake):
    w = Writer(store, api)
    t = store.task_open("Fix the tunnel", "cc-1", peer="deneb", name="fix-tunnel",
                        rc_url="https://claude.ai/code/session_x")["task_id"]
    assert w.step() > 0
    nid = card_note_id(t)
    assert fake.notes[nid]["parents"] == [ROOT_ID]
    got = labels(fake, nid)
    assert got["taskId"] == t and got["section"] == "working" and got["peer"] == "deneb"
    assert "status" not in got and "doneAt" not in got and "swarmCard" in got
    assert '"rc_url": "https://claude.ai/code/session_x"' in fake.notes[record_note_id(t)]["content"]
    assert "queueHeartbeat" in labels(fake, ROOT_ID)

    store.task_report(t, "cc-1", "blocked", "needs a key")
    w.step()
    got = labels(fake, nid)
    assert got["section"] == "needs_you" and got["status"] == "blocked"
    assert '"reason": "needs a key"' in fake.notes[record_note_id(t)]["content"]
    store.task_report(t, "cc-1", "working")
    w.step()
    assert "status" not in labels(fake, nid), "Working carries no status tag (R61 item 3)"
    store.task_report(t, "cc-1", "done")
    w.step()
    got = labels(fake, nid)
    assert got["section"] == "done" and int(got["doneAt"]) > 0
    assert w.step() == 0  # the mark moved past every event
    assert not any(n["title"] in ("Ledger", "Older") for n in fake.notes.values())


def test_proposed_body_is_tonys_and_spawn_checks_it(store, api, fake):
    w = Writer(store, api)
    store.body_reader = w.read_body
    t = store.card_action("note_new", None, {"title": "Rework the board"})["task_id"]
    w.step()
    nid = card_note_id(t)
    assert store.task_get(t)["body_ref"] == nid
    assert fake.notes[nid]["mime"] == "text/x-markdown"
    body = "# Rework\n\nFour sections, no drag.\n"
    fake.notes[nid]["content"] = body  # Tony writes the note in Trilium

    w.upsert(store.task_get(t))  # a later upsert leaves the body alone
    assert fake.notes[nid]["content"] == body

    with pytest.raises(QueueError, match="changed"):
        store.card_action("spawn", t, {"peer": "deneb",
                                       "body_sha256": hashlib.sha256(b"old").hexdigest()})
    out = store.card_action("spawn", t, {"peer": "deneb",
                                         "body_sha256": hashlib.sha256(body.encode()).hexdigest()})
    assert out["body"] == body and out["body_ref"] == nid
    w.step()
    assert labels(fake, nid)["section"] == "working" and fake.notes[nid]["content"] == body


def test_fold_removes_the_joiners_card(store, api, fake):
    w = Writer(store, api)
    a = store.task_open("Main task", "cc-a")["task_id"]
    b = store.task_open("Joiner's own", "cc-b")["task_id"]
    w.step()
    assert card_note_id(b) in fake.notes
    store.task_join(a, "cc-b")
    w.step()
    assert card_note_id(b) not in fake.notes
    assert '"cc-b"' in fake.notes[record_note_id(a)]["content"]


def test_board_install_is_idempotent(api, fake):
    out = board.install(api, "https://approve.test/")
    again = board.install(api, "https://approve.test")
    assert out == again
    root = fake.notes[ROOT_ID]
    assert root["relations"]["renderNote"] == board.PAGE_ID
    assert root["labels"]["approverOrigin"] == "https://approve.test"
    assert root["type"] == "render" and "swarmBoard" in root["labels"]
    assert fake.notes[board.SCRIPT_ID]["mime"] == "application/javascript;env=frontend"
    assert "awm-panel" in fake.notes[board.PAGE_ID]["content"]


def test_ops_post_rewrites_one_note_per_kind_and_node(store, api, fake):
    w = Writer(store, api)
    with pytest.raises(ValueError, match="Ops book"):
        w.post_ops("password", "mizar", "mizar login password", "x")
    fake.note_create("Ops", note_id="opsBook", labels={"swarmOps": ""})
    first = w.post_ops("password", "mizar", "mizar login password", "`aaa`")
    again = w.post_ops("password", "mizar", "mizar login password", "`bbb`")
    assert first == again
    note = fake.notes[first]
    assert note["title"] == "password - mizar" and "`bbb`" in note["content"]
    assert "agentManaged" in note["labels"] and note["mime"] == "text/x-markdown"
    assert w.post_ops("password", "alcor", "alcor login password", "`c`") != first


def test_the_writer_holds_no_vault_token():
    import inspect

    from awm.operations import trilium, writer
    for mod in (trilium, writer, board):
        assert "ETAPI_TOKEN" not in inspect.getsource(mod)


def test_messages_plan_and_todos_reach_the_card(tmp_path):
    api = FakeTrilium()
    store = Store(tmp_path / "q.db")
    w = Writer(store, TriliumTool(tool=api))
    t = store.task_open("deploy", "ccA")["task_id"]
    store.task_detail(t, plan="# Deploy", todos=[{"id": "1", "subject": "go", "status": "pending"}])
    store.task_message(t, "deploy passed", author="deploy", node="mizar")
    w.sync_once()
    record = json.loads(api.notes[record_note_id(t)]["content"])
    assert record["plan"] == "# Deploy" and record["todos"][0]["subject"] == "go"
    msgs = api.notes[board.messages_note_id(t)]
    assert msgs["parents"] == [card_note_id(t)] and "deploy passed" in msgs["content"]
    store.task_message(t, "second", author="tony")
    w.sync_once()
    assert "second" in api.notes[board.messages_note_id(t)]["content"]
