import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from verify_grant import b64u, canon, key_id

from awm.queuerelay.relay import Refused, Relay, Unreachable, order_loop, session_name

store_mod = pytest.importorskip("awm.taskqueue.store")


class FlakyStore:
    """The real store behind a link that can drop, and that can lose a reply after applying."""

    def __init__(self, store):
        self.store, self.up, self.lose_reply, self.calls = store, True, False, []

    async def call(self, verb, args):
        if not self.up:
            raise Unreachable("down")
        self.calls.append((verb, args.get("op_id")))
        try:
            res = getattr(self.store, verb)(**args)
        except store_mod.QueueError as exc:
            raise Refused(str(exc))
        if self.lose_reply:
            self.lose_reply = False
            raise Unreachable("reply lost")
        return res


@pytest.fixture
def pair(tmp_path):
    store = store_mod.Store(tmp_path / "q.db", action_secret=b"k")
    link = FlakyStore(store)
    return store, link, Relay("mizar", tmp_path / "r.db", link)


async def test_online_write_and_read(pair):
    store, link, relay = pair
    r = await relay.write("task_open", {"title": "x", "session_id": "ccA"})
    assert "queued" not in r and store.task_get(r["task_id"])["peer"] == "mizar"
    assert await relay.read("task_for_session", {"session_id": "ccA"}) == r["task_id"]


async def test_outbox_keeps_order_while_alcor_is_away(pair):
    store, link, relay = pair
    link.up = False
    r = await relay.write("task_open", {"title": "offline work", "session_id": "ccA"})
    assert r["queued"] and r["outbox"] == 1
    tid = r["task_id"]
    await relay.write("task_report", {"task_id": tid, "session_id": "ccA", "state": "blocked"})
    await relay.write("task_report", {"task_id": tid, "session_id": "ccA", "state": "review"})
    assert relay.status()["alcor_down_since"] is not None
    assert await relay.read("task_for_session", {"session_id": "ccA"}) == tid
    stale = await relay.read("task_get", {"task_id": tid})
    assert stale["stale"] is True
    link.up = True
    assert await relay.flush() == 3
    c = store.task_get(tid)
    assert (c["section"], c["status"]) == ("needs_you", "review")
    assert [v for v, _ in link.calls] == ["task_open", "task_report", "task_report"]
    assert relay.status() == {"node": "mizar", "outbox": 0, "refused": 0, "alcor_down_since": None}


async def test_lost_reply_replays_once(pair):
    store, link, relay = pair
    link.lose_reply = True
    r = await relay.write("task_open", {"title": "x", "session_id": "ccA"})
    assert r["queued"]
    await relay.flush()
    assert len(store.task_list()["cards"]) == 1
    ops = [op for _, op in link.calls]
    assert ops[0] == ops[1], "the replay reuses the op_id"


async def test_refused_write_surfaces_and_does_not_block(pair):
    store, link, relay = pair
    with pytest.raises(Refused):
        await relay.write("task_report", {"task_id": "nosuchid", "state": "done"})
    assert relay.status()["refused"] == 1 and relay.pending() == 0


def _order(sk, body):
    pub = sk.public_key()
    return {"order": body, "sig": b64u(sk.sign(canon(body))), "key_id": key_id(pub)}


async def test_spawn_orders_run_once_and_only_for_this_node(pair):
    _, _, relay = pair
    sk = Ed25519PrivateKey.generate()
    now = int(time.time())
    good = _order(sk, {"order_id": "o1", "kind": "spawn", "node": "mizar", "not_after": now + 600,
                       "payload": {"task_id": "ABEiM0RV", "title": "Fix the VPN MTU", "body": "see notes"}})
    other = _order(sk, {"order_id": "o2", "kind": "spawn", "node": "alcor", "not_after": now + 600,
                        "payload": {"task_id": "ABEiM0RV", "title": "x"}})
    forged = dict(good, order={**good["order"], "order_id": "o3"})
    batches = [[good, other, forged, good]]
    stop = asyncio.Event()
    started, done = [], []

    async def pull():
        if batches:
            return batches.pop()
        stop.set()
        return []

    async def start(args):
        started.append(args)
        return {"job": "j1"}

    async def fin(oid, ok, note):
        done.append((oid, ok))

    await order_loop(relay, pull, sk.public_key(), start, fin,
                     {"project": "awm", "scope": "intake"}, stop=stop)
    assert done == [("o1", True)]
    assert started[0]["name"] == "fix-the-vpn" and "ABEiM0RV" in started[0]["prompt"]
    assert started[0]["remote_control"] is True


def test_session_name():
    assert session_name("Redux: queue page!") == "redux-queue-page"
    assert session_name("") == "task"
