import time
import uuid

import pytest

from awm.taskqueue.ids import is_task_id, mint_task_id
from awm.taskqueue.store import QueueError, Store

SECRET = b"s3cret"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "q.db", action_secret=SECRET)


def act(store, action, task_id, args):
    ts = time.time()
    return store.card_action(action, task_id, args, ts, store.action_mac(action, task_id, args, ts))


def test_task_id_vector_shared_with_sessions():
    assert mint_task_id(uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")) == "ABEiM0RV"
    assert is_task_id(mint_task_id())
    assert not is_task_id("abc/defg")


def test_open_attach_join_and_sessions(store):
    t = store.task_open("redux queue", "ccA", node="mizar", rc_url="https://claude.ai/x")["task_id"]
    store.task_attach(t, "ccB", "worker", node="alcor")
    own = store.task_open("side quest", "ccC")["task_id"]
    j = store.task_join(t, "ccC")
    assert j == {"task_id": t, "main_session_id": "ccA", "folded_task_id": own}
    assert store.task_for_session("ccC") == t
    assert store.task_get(own)["task_id"] == t, "a folded card resolves to its target"
    roles = {s["session_id"]: s["role"] for s in store.task_sessions(t)}
    assert roles == {"ccA": "main", "ccB": "worker", "ccC": "joined"}
    assert store.task_get(t)["peer"] == "mizar"
    store.task_session_end(t, "ccB")
    active = {s["session_id"]: s["active"] for s in store.task_sessions(t)}
    assert active["ccB"] is False and active["ccA"] is True
    assert own not in [c["task_id"] for c in store.task_list()["cards"]]


def test_report_moves_sections_and_working_has_no_tag(store):
    t = store.task_open("x", "ccA")["task_id"]
    assert store.task_get(t)["section"] == "working" and store.task_get(t)["status"] is None
    store.task_report(t, "ccA", "blocked", "need a key")
    c = store.task_get(t)
    assert (c["section"], c["status"]) == ("needs_you", "blocked")
    store.task_report(t, "ccA", "review")
    assert store.task_get(t)["status"] == "review"
    store.task_report(t, "ccA", "done")
    c = store.task_get(t)
    assert (c["section"], c["status"]) == ("done", "done") and c["done_at"]
    with pytest.raises(QueueError):
        store.task_report(t, "ccA", "dragged")


def test_replayed_op_applies_once(store):
    a = store.task_open("x", "ccA", op_id="mizar:1")
    b = store.task_open("x", "ccA", op_id="mizar:1")
    assert a == b
    assert len(store.task_list()["cards"]) == 1


def test_relay_minted_id_is_kept_and_taken_id_refused(store):
    tid = mint_task_id()
    assert store.task_open("offline", "ccA", task_id=tid)["task_id"] == tid
    with pytest.raises(QueueError):
        store.task_open("other", "ccB", task_id=tid)


def test_actions_need_the_approver_mac(store):
    t = store.task_open("x", "ccA")["task_id"]
    ts = time.time()
    with pytest.raises(QueueError, match="MAC"):
        store.card_action("close", t, {}, ts, "0" * 64)
    mac = store.action_mac("close", t, {}, ts)
    store.card_action("close", t, {}, ts, mac)
    with pytest.raises(QueueError, match="already"):
        store.card_action("close", t, {}, ts, mac)
    old = ts - 600
    with pytest.raises(QueueError, match="stale"):
        store.card_action("reject", t, {}, old, store.action_mac("reject", t, {}, old))


def test_note_spawn_newfrom_reject(store):
    n = act(store, "note_new", None, {"title": "idea"})["task_id"]
    c = store.task_get(n)
    assert (c["section"], c["kind"]) == ("proposed", "note")
    with pytest.raises(QueueError):
        act(store, "spawn", n, {})
    s = act(store, "spawn", n, {"peer": "alcor", "body_sha256": "ab"})
    assert s["peer"] == "alcor" and store.task_get(n)["section"] == "working"
    with pytest.raises(QueueError, match="Proposed"):
        act(store, "spawn", n, {"peer": "alcor"})
    f = act(store, "new_from", n, {})
    assert store.task_get(f["task_id"])["parent_task"] == n
    act(store, "reject", f["task_id"], {})
    assert store.task_get(f["task_id"])["status"] == "rejected"


def test_approval_cards(store):
    ts = time.time()
    args = {"request_id": "r1", "title": "admin ssh on capella", "node": "capella", "meta": {"kind": "admin_ssh"}}
    mac = store.action_mac("approval_card", None, args, ts)
    t = store.approval_card("r1", "admin ssh on capella", node="capella", meta={"kind": "admin_ssh"}, mac=mac, ts=ts)["task_id"]
    c = store.task_get(t)
    assert (c["section"], c["status"], c["kind"]) == ("needs_you", "approval", "approval")
    with pytest.raises(QueueError):
        store.task_report(t, None, "done")
    with pytest.raises(QueueError):
        act(store, "close", t, {})
    mac = store.action_mac("approval_resolve", t, {"outcome": "expired"}, ts)
    store.approval_resolve(t, "expired", mac=mac, ts=ts)
    assert store.task_get(t)["status"] == "expired"


def test_done_pages_back_and_search(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path / "q.db", action_secret=SECRET, clock=lambda: clock[0])
    ids = []
    for i in range(7):
        clock[0] += 1
        t = store.task_open(f"job {i}", f"cc{i}")["task_id"]
        store.task_report(t, f"cc{i}", "done")
        ids.append(t)
    p1 = store.task_list("done", limit=3)
    assert [c["title"] for c in p1["cards"]] == ["job 6", "job 5", "job 4"]
    p2 = store.task_list("done", limit=3, before=p1["next_before"])
    assert [c["title"] for c in p2["cards"]] == ["job 3", "job 2", "job 1"]
    p3 = store.task_list("done", limit=3, before=p2["next_before"])
    assert [c["title"] for c in p3["cards"]] == ["job 0"] and p3["next_before"] is None
    assert [c["title"] for c in store.task_list(q="job 4")["cards"]] == ["job 4"]


def test_events_feed(store):
    t = store.task_open("x", "ccA")["task_id"]
    store.task_report(t, "ccA", "blocked")
    ev = store.events_since(0)
    assert [e["kind"] for e in ev] == ["open", "session", "report"]
    assert store.events_since(ev[-1]["id"]) == []
