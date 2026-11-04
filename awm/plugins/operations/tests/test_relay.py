import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from verify_grant import b64u, canon, key_id

from awm.operations.home import Home
from awm.operations.relay import Refused, Relay, Unreachable, order_loop, session_name

ME = "cc" + "1" * 32


class FlakyLink:
    """alcor's Home behind a link that can drop, and that can lose a reply after applying."""

    def __init__(self, home):
        self.home, self.up, self.lose_reply, self.calls = home, True, False, []

    async def call(self, verb, args):
        if not self.up:
            raise Unreachable("down")
        self.calls.append((verb, args.get("op_id")))
        try:
            res = await self.home.call(verb, args, node="mizar")
        except ValueError as exc:
            raise Refused(str(exc))
        if self.lose_reply:
            self.lose_reply = False
            raise Unreachable("reply lost")
        return res


@pytest.fixture
def pair(tmp_path, env):
    ap, store, _ = env
    link = FlakyLink(Home(store, ap))
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
    assert started[0]["task_id"] == "ABEiM0RV" and started[0]["role"] == "main"
    assert started[0]["remote_control"] is True


def test_session_name():
    assert session_name("Redux: queue page!") == "redux-queue-page"
    assert session_name("") == "task"


async def test_approvals_ride_the_outbox(pair, env):
    """A request made while alcor is away keeps its id, and only its session cancels it (R97 item 1)."""
    ap, _, _ = env
    store, link, relay = pair
    link.up = False
    r = await relay.write("approval_request", {"kind": "vault_hold", "node": "deneb",
                                               "reason": "checks tripped", "session_id": ME})
    assert r["queued"] and r["request_id"].startswith("q")
    with pytest.raises(Unreachable):
        await relay.poll("approval_wait", {"request_id": r["request_id"], "wait_s": 0})
    await relay.write("approval_cancel", {"request_id": r["request_id"], "session_id": ME})
    link.up = True
    assert await relay.flush() == 2
    got = ap.approval_get(r["request_id"])
    assert got["status"] == "cancelled" and got["requester_session"] == ME
    assert store.task_get(got["task_id"])["status"] == "cancelled"


async def test_a_lost_approval_reply_replays_once(pair, env):
    ap, _, _ = env
    _, link, relay = pair
    link.lose_reply = True
    r = await relay.write("approval_request", {"kind": "vault_hold", "reason": "r",
                                               "session_id": ME})
    assert r["queued"]
    await relay.flush()
    assert relay.status()["refused"] == 0
    assert ap.approval_get(r["request_id"])["status"] == "pending"


async def test_cancel_by_another_session_is_refused(pair, env):
    _, _, relay = pair
    r = await relay.write("approval_request", {"kind": "vault_hold", "reason": "r",
                                               "session_id": ME})
    with pytest.raises(Refused, match="not_requester"):
        await relay.write("approval_cancel", {"request_id": r["request_id"],
                                              "session_id": "cc" + "2" * 32})
