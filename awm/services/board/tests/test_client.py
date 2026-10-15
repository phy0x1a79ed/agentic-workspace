"""BoardClient against a stub host: the verbs, the error mapping, and the reconnecting stream."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from door_stubs import Live  # noqa: E402

from awm.board.client import BoardClient, BoardConflict, BoardError, BoardRefused  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def client_for(handler, **kwargs) -> BoardClient:
    return BoardClient("http://host/", "secret", transport=httpx.MockTransport(handler),
                       backoff_start=0.01, backoff_max=0.02, **kwargs)


def sse(*events: tuple[int, str, dict], comments: bool = True) -> list[bytes]:
    chunks = [b"retry: 3000\n\n"]
    for event_id, kind, card in events:
        if comments:
            chunks.append(b": keepalive\n\n")
        chunks.append(f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(card)}\n\n".encode())
    return chunks


async def chunked(chunks: list[bytes]):
    for chunk in chunks:
        yield chunk


async def test_verbs_hit_the_door_with_the_bearer_and_the_right_shape():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "c1"} if request.url.path != "/board/cards" or request.method == "POST" else [])

    async with client_for(handler) as client:
        await client.post(kind="request", recipient="beta", title="t", body="b", reply_to="r")
        await client.claim("c1")
        await client.complete("c1", "done")
        await client.fail("c1", "why")
        await client.get("c1")
        await client.list(status="posted", sender="", kind="request")

    assert all(r.headers["authorization"] == "Bearer secret" for r in seen)
    shapes = [(r.method, r.url.path) for r in seen]
    assert shapes == [
        ("POST", "/board/cards"),
        ("POST", "/board/cards/c1/claim"),
        ("POST", "/board/cards/c1/complete"),
        ("POST", "/board/cards/c1/fail"),
        ("GET", "/board/cards/c1"),
        ("GET", "/board/cards"),
    ]
    assert json.loads(seen[0].content) == {
        "kind": "request", "recipient": "beta", "title": "t", "body": "b",
        "priority": "normal", "reply_to": "r"}
    assert json.loads(seen[2].content) == {"result": "done"}
    assert json.loads(seen[3].content) == {"reason": "why"}
    assert dict(seen[5].url.params) == {"status": "posted", "kind": "request"}  # empty filters are dropped


async def test_list_sends_paging_and_drops_empty_values():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with client_for(handler) as client:
        await client.list(status="posted", limit=200, offset=400)
        await client.list(limit=50, offset=0)
        await client.list()
    assert dict(seen[0].url.params) == {"status": "posted", "limit": "200", "offset": "400"}
    assert dict(seen[1].url.params) == {"limit": "50"}
    assert dict(seen[2].url.params) == {}


async def test_the_stream_reports_when_it_is_attached_and_when_it_is_not():
    states: list[bool] = []
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(502)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=chunked(sse((1, "card.posted", {"id": "a"}))))

    async with client_for(handler) as client:
        events = client.stream(on_state=states.append)
        await asyncio.wait_for(anext(events), 3)
        live = list(states)
        await events.aclose()
    assert live == [False, True]  # refused once, then attached
    assert states[-1] is False  # closing the stream detaches it


async def test_404_and_409_are_their_own_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(409, json={"error": "the card is already held"})
        if request.url.path.endswith("/boom"):
            return httpx.Response(500, text="oops")
        return httpx.Response(404, json={"error": "not found"})

    async with client_for(handler) as client:
        with pytest.raises(BoardConflict) as held:
            await client.claim("c1")
        assert held.value.status == 409
        with pytest.raises(BoardRefused):
            await client.get("c1")
        with pytest.raises(BoardError) as other:
            await client._send("GET", "/boom")
        assert other.value.status == 500
        assert not isinstance(other.value, (BoardRefused, BoardConflict))


async def test_the_stream_yields_events_and_ignores_heartbeats():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=chunked(sse((1, "card.posted", {"id": "a"}), (2, "card.claimed", {"id": "a"}))))

    async with client_for(handler) as client:
        events = client.stream()
        first = await anext(events)
        second = await anext(events)
        await events.aclose()
    assert first == (1, "card.posted", {"id": "a"})
    assert second == (2, "card.claimed", {"id": "a"})


async def test_the_stream_reconnects_from_the_last_id_it_saw():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:  # the server drops the connection after two events
            body = sse((5, "card.posted", {"id": "a"}), (6, "card.posted", {"id": "b"}))
        elif len(requests) == 2:  # a gateway error, then the stream is back
            return httpx.Response(502)
        else:  # a replay that overlaps what the client already has
            body = sse((6, "card.posted", {"id": "b"}), (7, "card.completed", {"id": "a"}))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunked(body))

    async with client_for(handler) as client:
        events = client.stream(last_event_id=4)
        got = [await asyncio.wait_for(anext(events), 3) for _ in range(3)]
        await events.aclose()

    assert [g[0] for g in got] == [5, 6, 7]  # 6 was not delivered twice
    assert [r.headers.get("last-event-id") for r in requests[:3]] == ["4", "6", "6"]


async def test_the_stream_retries_when_the_connection_fails():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, content=chunked(sse((1, "card.posted", {"id": "a"}))))

    async with client_for(handler) as client:
        events = client.stream()
        assert (await asyncio.wait_for(anext(events), 3))[0] == 1
        await events.aclose()
    assert len(attempts) == 3


async def test_a_bare_id_from_the_server_becomes_the_cursor_for_the_next_connect():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:  # a live start: only the head, then the connection drops
            body = [b"retry: 3000\n\n", b"id: 41\n\n"]
        else:
            body = sse((42, "card.posted", {"id": "a"}))
        return httpx.Response(200, content=chunked(body))

    async with client_for(handler) as client:
        events = client.stream()
        got = await asyncio.wait_for(anext(events), 3)
        await events.aclose()
    assert got[0] == 42  # the bare id was not surfaced as an event
    assert requests[0].headers.get("last-event-id") is None
    assert requests[1].headers.get("last-event-id") == "41"


async def test_a_resync_frame_is_surfaced_and_moves_the_cursor_even_backwards():
    requests: list[httpx.Request] = []
    resync = b'id: 7\nevent: resync\ndata: {"latest":7,"oldest":3}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            body = [resync]  # the client had asked from 99, past the server's head
        else:
            body = sse((8, "card.posted", {"id": "a"}))
        return httpx.Response(200, content=chunked(body))

    async with client_for(handler) as client:
        events = client.stream(last_event_id=99)
        first = await asyncio.wait_for(anext(events), 3)
        second = await asyncio.wait_for(anext(events), 3)
        await events.aclose()
    assert first == (7, "resync", {"latest": 7, "oldest": 3})
    assert second[0] == 8
    assert requests[0].headers["last-event-id"] == "99"
    assert requests[1].headers["last-event-id"] == "7"


async def test_a_refused_token_ends_the_stream_instead_of_retrying_forever():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json={"error": "not found"})

    async with client_for(handler) as client:
        with pytest.raises(BoardRefused):
            await anext(client.stream())
    assert len(attempts) == 1


async def test_a_bad_json_event_is_dropped_and_the_stream_goes_on():
    body = [b"id: 1\nevent: card.posted\ndata: {not json\n\n"] + sse((2, "card.posted", {"id": "ok"}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunked(body))

    async with client_for(handler) as client:
        events = client.stream()
        assert (await anext(events))[0] == 2
        await events.aclose()


# -- against the real door, with a stub board behind it ---------------------------


@pytest.fixture
async def live():
    server = Live(heartbeat_s=0.2, poll_s=0.05)
    await server.start()
    yield server
    await server.stop()


async def test_a_swarm_posts_and_the_other_hears_it_on_the_stream(live):
    _, alpha_token = live.parties.add("alpha", "p", "domestic")
    _, beta_token = live.parties.add("beta", "q", "domestic")

    async with BoardClient(live.url, alpha_token) as alpha, BoardClient(live.url, beta_token) as beta:
        events = beta.stream()
        pending = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.2)  # the live stream has started from the log's head
        card = await alpha.post(kind="request", recipient="beta", title="help", body="please")
        event_id, kind, seen = await asyncio.wait_for(pending, 5)
        await events.aclose()

        assert (kind, seen["id"]) == ("card.posted", card["id"])
        assert seen["sender"]["swarm"] == "alpha"

        claimed = await beta.claim(card["id"])
        assert claimed["claimant"] == "beta"
        with pytest.raises(BoardConflict):
            await beta.claim(card["id"])
        done = await beta.complete(card["id"], "fixed")
        assert done["status"] == "done"
        assert [c["id"] for c in await alpha.list(status="done")] == [card["id"]]

        # The sender hears every change to its own card, from where it left off.
        replay = alpha.stream(last_event_id=0)
        kinds = [(await asyncio.wait_for(anext(replay), 5))[1] for _ in range(3)]
        await replay.aclose()
        assert kinds == ["card.posted", "card.claimed", "card.completed"]


async def test_the_real_door_resyncs_a_client_whose_cursor_is_past_its_head(live):
    _, token = live.parties.add("alpha", "p", "domestic")
    live.events.append("card.posted", card_for("alpha"))
    async with BoardClient(live.url, token) as client:
        events = client.stream(last_event_id=500)
        kind = (await asyncio.wait_for(anext(events), 5))[1]
        await events.aclose()
    assert kind == "resync"


def card_for(swarm: str) -> dict:
    return {"id": "x", "sender": {"swarm": swarm, "principal": "p", "party": "z"},
            "recipient": "beta", "status": "posted"}


async def test_a_revoked_token_is_refused_by_the_real_door(live):
    row, token = live.parties.add("alpha", "p", "domestic")
    live.parties.revoke(row["party_id"])
    async with BoardClient(live.url, token) as client:
        with pytest.raises(BoardRefused):
            await client.list()
