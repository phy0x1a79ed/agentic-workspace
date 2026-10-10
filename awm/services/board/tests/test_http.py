"""The door's contract: one answer for every refusal, and the bearer as the only identity."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from door_stubs import StubBoard, StubEvents, StubParties  # noqa: E402

from awm.board.http import create_app  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Door:
    def __init__(self) -> None:
        self.parties = StubParties()
        self.events = StubEvents()
        self.board = StubBoard(self.events, claim_delay=0.05)
        self.app = create_app(self.board, self.parties, self.events)
        self.tokens: dict[str, str] = {}

    def party(self, swarm: str, principal: str = "p", relation: str = "domestic") -> str:
        row, token = self.parties.add(swarm, principal, relation)
        self.tokens[swarm] = token
        return token

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://board", headers=headers
        )


@pytest.fixture
def door() -> Door:
    d = Door()
    d.party("alpha")
    d.party("beta")
    d.party("gamma")
    return d


CARD = {"kind": "request", "recipient": "beta", "title": "t", "body": "b"}


async def test_a_bad_token_answers_404_on_every_route(door):
    card_id = str(uuid.uuid4())
    routes = [
        ("POST", "/board/cards"),
        ("GET", "/board/cards"),
        ("GET", f"/board/cards/{card_id}"),
        ("POST", f"/board/cards/{card_id}/claim"),
        ("POST", f"/board/cards/{card_id}/complete"),
        ("POST", f"/board/cards/{card_id}/fail"),
        ("GET", "/board/stream"),
    ]
    for token in ("wrong", "", None):
        async with door.client(token) as client:
            for method, path in routes:
                response = await client.request(method, path, json=CARD if method == "POST" else None)
                assert response.status_code == 404, (token, method, path)
                assert response.json() == {"error": "not found"}


async def test_a_revoked_token_answers_404(door):
    async with door.client(door.tokens["alpha"]) as client:
        assert (await client.get("/board/cards")).status_code == 200
        party_id = next(r["party_id"] for r in door.parties.rows.values() if r["swarm"] == "alpha")
        door.parties.revoke(party_id)
        assert (await client.get("/board/cards")).status_code == 404


async def test_unknown_paths_and_wrong_methods_answer_404(door):
    async with door.client(door.tokens["alpha"]) as client:
        assert (await client.get("/board/nothing")).status_code == 404
        assert (await client.get("/elsewhere")).status_code == 404
        assert (await client.delete("/board/cards")).status_code == 404
        assert (await client.get("/board/cards/not-a-uuid")).status_code == 404


async def test_a_card_the_caller_may_not_see_is_indistinguishable_from_a_missing_one(door):
    async with door.client(door.tokens["alpha"]) as alpha:
        card = (await alpha.post("/board/cards", json=CARD)).json()
    async with door.client(door.tokens["gamma"]) as gamma:
        hidden = await gamma.get(f"/board/cards/{card['id']}")
        missing = await gamma.get(f"/board/cards/{uuid.uuid4()}")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


async def test_a_swarm_that_may_see_but_not_claim_is_refused_with_404(door):
    async with door.client(door.tokens["alpha"]) as alpha:
        card = (await alpha.post("/board/cards", json=CARD)).json()
        # The sender sees its own card but it is addressed to beta.
        assert (await alpha.post(f"/board/cards/{card['id']}/claim")).status_code == 404


async def test_the_sender_comes_from_the_bearer_not_the_body(door):
    forged = {**CARD, "sender": {"swarm": "gamma", "principal": "root", "party": "x"}}
    async with door.client(door.tokens["alpha"]) as client:
        response = await client.post("/board/cards", json=forged)
    assert response.status_code == 200
    sender = response.json()["sender"]
    assert sender["swarm"] == "alpha"
    assert sender["principal"] == "p"
    assert door.board.last_post_party["swarm"] == "alpha"


async def test_a_malformed_post_is_a_400_for_an_authenticated_caller(door):
    async with door.client(door.tokens["alpha"]) as client:
        assert (await client.post("/board/cards", json={"kind": "request"})).status_code == 400
        assert (await client.post("/board/cards", content=b"nope")).status_code == 400
        assert (await client.post("/board/cards", json={**CARD, "kind": "quest"})).status_code == 400


async def test_the_claim_race_gives_one_200_and_one_409(door):
    async with door.client(door.tokens["alpha"]) as alpha:
        card = (await alpha.post("/board/cards", json={**CARD, "recipient": "open"})).json()
    path = f"/board/cards/{card['id']}/claim"
    async with door.client(door.tokens["beta"]) as beta, door.client(door.tokens["gamma"]) as gamma:
        first, second = await asyncio.gather(beta.post(path), gamma.post(path))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first if first.status_code == 200 else second
    assert winner.json()["status"] == "in_progress"


async def test_many_simultaneous_claims_still_have_one_winner(door):
    async with door.client(door.tokens["alpha"]) as alpha:
        card = (await alpha.post("/board/cards", json={**CARD, "recipient": "open"})).json()
    path = f"/board/cards/{card['id']}/claim"
    tokens = [door.tokens["beta"], door.tokens["gamma"]] * 4
    clients = [door.client(t) for t in tokens]
    try:
        results = await asyncio.gather(*(c.post(path) for c in clients))
    finally:
        for c in clients:
            await c.aclose()
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1
    assert codes.count(409) == len(codes) - 1


async def test_only_the_claimant_completes_or_fails(door):
    async with door.client(door.tokens["alpha"]) as alpha, door.client(door.tokens["beta"]) as beta:
        card = (await alpha.post("/board/cards", json=CARD)).json()
        base = f"/board/cards/{card['id']}"
        assert (await beta.post(f"{base}/claim")).status_code == 200
        assert (await alpha.post(f"{base}/complete", json={"result": "x"})).status_code == 404
        done = await beta.post(f"{base}/complete", json={"result": "all good"})
        assert done.status_code == 200
        assert done.json()["status"] == "done"
        assert done.json()["result"] == "all good"

        second = (await alpha.post("/board/cards", json=CARD)).json()
        await beta.post(f"/board/cards/{second['id']}/claim")
        failed = await beta.post(f"/board/cards/{second['id']}/fail", json={"reason": "nope"})
        assert failed.json()["status"] == "failed"
        assert failed.json()["result"] == "nope"


async def test_list_passes_only_the_filters_that_are_set(door):
    async with door.client(door.tokens["alpha"]) as alpha, door.client(door.tokens["beta"]) as beta:
        await alpha.post("/board/cards", json=CARD)
        await alpha.post("/board/cards", json={**CARD, "kind": "message"})
        everything = (await beta.get("/board/cards")).json()
        requests = (await beta.get("/board/cards", params={"kind": "request", "sender": "alpha"})).json()
        none = (await beta.get("/board/cards", params={"status": "done"})).json()
    assert len(everything) == 2
    assert [c["kind"] for c in requests] == ["request"]
    assert none == []


async def test_an_unexpected_failure_answers_500_with_the_minimal_body(door, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("secret internals")

    door.board.list = explode
    async with door.client(door.tokens["alpha"]) as client:
        response = await client.get("/board/cards")
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}
    assert "secret internals" not in response.text
    assert "unhandled error" in caplog.text


async def test_a_vault_failure_is_a_503_and_a_value_error_a_400(door):
    from awm.board.vault import VaultError

    def down(*args, **kwargs):
        raise VaultError("trilium is gone")

    door.board.get = down
    async with door.client(door.tokens["alpha"]) as client:
        response = await client.get(f"/board/cards/{uuid.uuid4().hex}")
        assert response.status_code == 503
        assert response.json() == {"error": "vault unavailable"}
        assert (await client.post("/board/cards", json={**CARD, "kind": "quest"})).status_code == 400
