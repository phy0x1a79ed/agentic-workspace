"""The ``board`` domain: its declared surface, the two roles, and the relay to a host."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from door_stubs import Live, StubParties  # noqa: E402

from awm.board import hub_adapter  # noqa: E402
from awm.config import verb_effect  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

EFFECTS = {
    "post": "queue", "claim": "queue", "complete": "queue", "fail": "queue",
    "get": "read", "list": "read",
    "party_add": "secret", "party_revoke": "write", "party_list": "read",
}


def functions() -> dict[str, dict]:
    return {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}


def test_every_verb_declares_the_effect_the_plan_gives_it():
    declared = functions()
    assert set(declared) == set(EFFECTS)
    for name, effect in EFFECTS.items():
        assert "effect" in declared[name], f"{name} declares no effect"
        assert verb_effect(declared[name]) == effect


def test_tool_names_fold_into_one_board_domain_and_every_verb_has_a_handler():
    for name, spec in functions().items():
        assert spec["tool"] == f"board_{name}"
        assert spec["tool"].split("_", 1)[0] == "board"
        assert name in hub_adapter.HANDLERS


def test_required_params_are_declared_where_a_handler_indexes_them():
    declared = functions()
    for name in ("post",):
        required = {p["name"] for p in declared[name]["params"] if p.get("required")}
        assert required == {"kind", "recipient", "title"}
    for name in ("claim", "complete", "fail", "get"):
        assert [p["name"] for p in declared[name]["params"] if p.get("required")] == ["card_id"]


def test_the_role_defaults_to_client_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("AWM_BOARD_ROLE", raising=False)
    assert hub_adapter.role() == "client"
    monkeypatch.setenv("AWM_BOARD_ROLE", "HOST")
    assert hub_adapter.role() == "host"
    monkeypatch.setenv("AWM_BOARD_ROLE", "relay")
    with pytest.raises(ValueError):
        hub_adapter.role()


# -- host-only verbs --------------------------------------------------------------


@pytest.mark.parametrize("verb,args", [
    ("party_add", {"swarm": "alpha", "principal": "p", "relation": "domestic"}),
    ("party_revoke", {"party_id": "x"}),
    ("party_list", {}),
])
async def test_the_client_role_refuses_the_host_only_verbs(monkeypatch, verb, args):
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setattr(hub_adapter, "HOST", SimpleNamespace(parties=StubParties()))
    reply = await hub_adapter.HANDLERS[verb](args, None)
    assert reply["ok"] is False
    assert "host only" in reply["error"]
    assert not hub_adapter.HOST.parties.rows


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "host")
    state = SimpleNamespace(parties=StubParties())
    monkeypatch.setattr(hub_adapter, "HOST", state)
    return state


async def test_party_add_shows_the_token_once_and_list_never_shows_it(host):
    added = await hub_adapter.party_add({"swarm": "alpha", "principal": "p", "relation": "domestic"})
    assert added["ok"] and added["token"]
    assert "token_hash" not in added["party"]
    assert host.parties.resolve(added["token"])["swarm"] == "alpha"

    listed = await hub_adapter.party_list({})
    assert [p["swarm"] for p in listed["parties"]] == ["alpha"]
    assert all("token_hash" not in p and "token" not in p for p in listed["parties"])


async def test_party_revoke_cuts_the_token_off(host):
    added = await hub_adapter.party_add({"swarm": "alpha", "principal": "p", "relation": "domestic"})
    revoked = await hub_adapter.party_revoke({"party_id": added["party"]["party_id"]})
    assert revoked["ok"] and revoked["party"]["revoked"] is True
    assert host.parties.resolve(added["token"]) is None

    missing = await hub_adapter.party_revoke({"party_id": "nope"})
    assert missing["ok"] is False


@pytest.mark.parametrize("caller", ["peer", "peer:shaula", "user:x", "user", "anything"])
async def test_admin_verbs_refuse_every_stamped_caller(host, caller):
    """Only the host's own bare CLI call passes: an edge user or a mesh peer carries an identity."""
    args = {"swarm": "alpha", "principal": "p", "relation": "domestic"}
    for reply in (
        await hub_adapter.party_add(args, caller),
        await hub_adapter.party_list({}, caller),
        await hub_adapter.party_revoke({"party_id": "x"}, caller),
    ):
        assert reply["ok"] is False
    assert not host.parties.rows


async def test_admin_verbs_accept_the_hosts_own_bare_call(host):
    args = {"swarm": "alpha", "principal": "p", "relation": "domestic"}
    assert (await hub_adapter.party_add(args, None))["ok"] is True
    assert (await hub_adapter.party_list({}, None))["ok"] is True


async def test_admin_verbs_say_so_while_the_host_is_still_starting(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "host")
    monkeypatch.setattr(hub_adapter, "HOST", None)
    reply = await hub_adapter.party_list({})
    assert reply["ok"] is False and "starting" in reply["error"]


# -- the relay ----------------------------------------------------------------------


@pytest.fixture
async def relay(monkeypatch):
    """A client-role node whose env points at a stub host."""
    server = Live()
    await server.start()
    _, token = server.parties.add("alpha", "p", "domestic")
    _, other = server.parties.add("beta", "q", "domestic")
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setenv("AWM_BOARD_URL", server.url)
    monkeypatch.setenv("AWM_BOARD_TOKEN", token)
    server.beta_token = other
    yield server
    await server.stop()


async def test_the_card_verbs_relay_to_the_host_and_the_sender_is_the_swarm_token(relay):
    posted = await hub_adapter.post({"kind": "request", "recipient": "beta", "title": "t", "body": "b"})
    assert posted["ok"] is True
    card = posted["card"]
    assert card["sender"]["swarm"] == "alpha"

    got = await hub_adapter.get({"card_id": card["id"]})
    assert got["card"]["id"] == card["id"]
    listed = await hub_adapter.list_cards({"recipient": "beta"})
    assert [c["id"] for c in listed["cards"]] == [card["id"]]


async def test_claim_complete_and_fail_relay_with_the_hosts_status(relay, monkeypatch):
    posted = (await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "t"}))["card"]
    claimed = await hub_adapter.claim({"card_id": posted["id"]})
    assert claimed["ok"] and claimed["card"]["status"] == "in_progress"

    again = await hub_adapter.claim({"card_id": posted["id"]})
    assert again["ok"] is False and again["status"] == 409

    done = await hub_adapter.complete({"card_id": posted["id"], "result": "yes"})
    assert done["card"]["status"] == "done"

    second = (await hub_adapter.post({"kind": "request", "recipient": "alpha", "title": "u"}))["card"]
    await hub_adapter.claim({"card_id": second["id"]})
    failed = await hub_adapter.fail({"card_id": second["id"], "reason": "no"})
    assert failed["card"]["status"] == "failed"


@pytest.mark.parametrize("caller", ["peer", "peer:shaula", "peer:capella"])
async def test_card_verbs_refuse_a_caller_from_another_node(relay, caller):
    """The node's swarm token is the board identity; a mesh caller must not borrow it."""
    calls = [
        hub_adapter.post({"kind": "message", "recipient": "beta", "title": "t"}, caller),
        hub_adapter.claim({"card_id": "c"}, caller),
        hub_adapter.complete({"card_id": "c", "result": "r"}, caller),
        hub_adapter.fail({"card_id": "c", "reason": "r"}, caller),
        hub_adapter.get({"card_id": "c"}, caller),
        hub_adapter.list_cards({}, caller),
    ]
    for reply in await asyncio.gather(*calls):
        assert reply["ok"] is False
        assert "another node" in reply["error"]
    assert not relay.board.cards  # nothing reached the host


@pytest.mark.parametrize("caller", [None, "user:x"])
async def test_card_verbs_serve_local_agents_and_local_users(relay, caller):
    posted = await hub_adapter.post({"kind": "message", "recipient": "beta", "title": "t"}, caller)
    assert posted["ok"] is True
    assert (await hub_adapter.list_cards({}, caller))["ok"] is True


async def test_a_supervised_respawn_reuses_the_host_state(monkeypatch):
    built = []

    class FakeState:
        def __init__(self, directory):
            built.append(directory)
            self.app, self.board, self.events = object(), object(), object()

    served = []

    async def fake_serve(app, host, port, **kw):
        served.append(1)
        raise RuntimeError("door died")

    async def fake_maintain(board, events):
        await asyncio.Event().wait()

    monkeypatch.setattr(hub_adapter, "HOST", None)
    monkeypatch.setattr(hub_adapter, "HostState", FakeState)
    from awm.board import http, stream
    monkeypatch.setattr(http, "serve", fake_serve)
    monkeypatch.setattr(stream, "maintain", fake_maintain)
    for _ in range(2):  # the supervisor calls the factory again after each death
        with pytest.raises(BaseException):
            await hub_adapter._serve_host()
    assert len(built) == 1
    assert len(served) == 2


async def test_a_refusal_at_the_host_comes_back_as_a_reply_not_an_exception(relay, monkeypatch):
    monkeypatch.setenv("AWM_BOARD_TOKEN", "wrong")
    reply = await hub_adapter.get({"card_id": "5e0a1b06-0000-4000-8000-000000000000"})
    assert reply["ok"] is False and reply["status"] == 404


async def test_a_reply_never_carries_the_swarm_token(relay):
    secret = os.environ["AWM_BOARD_TOKEN"]
    replies = [
        await hub_adapter.post({"kind": "message", "recipient": "beta", "title": "t"}),
        await hub_adapter.list_cards({}),
        await hub_adapter.get({"card_id": "5e0a1b06-0000-4000-8000-000000000000"}),
    ]
    assert all(secret not in repr(r) for r in replies)


async def test_a_node_without_credentials_gets_an_explanation(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.delenv("AWM_BOARD_URL", raising=False)
    monkeypatch.delenv("AWM_BOARD_TOKEN", raising=False)
    assert "AWM_BOARD_URL" in (await hub_adapter.list_cards({}))["error"]
    monkeypatch.setenv("AWM_BOARD_URL", "http://127.0.0.1:9")
    assert "AWM_BOARD_TOKEN" in (await hub_adapter.list_cards({}))["error"]


async def test_an_unreachable_host_is_an_answer(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setenv("AWM_BOARD_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AWM_BOARD_TOKEN", "t")
    reply = await hub_adapter.list_cards({})
    assert reply["ok"] is False and "unreachable" in reply["error"]


async def test_the_host_relays_to_its_own_loopback_door_by_default(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "host")
    monkeypatch.delenv("AWM_BOARD_URL", raising=False)
    monkeypatch.setenv("AWM_BOARD_PORT", "12999")
    monkeypatch.setenv("AWM_BOARD_TOKEN", "t")
    assert hub_adapter._door_target() == ("http://127.0.0.1:12999", "t")


# -- startup ---------------------------------------------------------------------------


async def test_a_client_starts_no_door(monkeypatch):
    monkeypatch.setenv("AWM_BOARD_ROLE", "client")
    monkeypatch.setattr(hub_adapter, "SERVING", None)
    assert hub_adapter._on_start() is None
    assert hub_adapter.SERVING is None


async def test_a_host_schedules_its_door_and_returns_at_once(monkeypatch):
    started = asyncio.Event()

    async def fake_serve():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setenv("AWM_BOARD_ROLE", "host")
    monkeypatch.setattr(hub_adapter, "_serve_host", fake_serve)
    monkeypatch.setattr(hub_adapter, "SERVING", None)
    assert hub_adapter._on_start() is None
    try:
        await asyncio.wait_for(started.wait(), 2)
    finally:
        hub_adapter.SERVING.cancel()


def test_a_second_host_process_refuses_to_start(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_adapter, "_LOCK_FD", None)
    hub_adapter.hold_single_instance(tmp_path)
    first = hub_adapter._LOCK_FD
    try:
        with pytest.raises(SystemExit):
            hub_adapter.hold_single_instance(tmp_path)
    finally:
        os.close(first)
