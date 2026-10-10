"""In-memory stand-ins for the board's stores, for the door-side tests.

The door, the stream, the client and the adapter are tested against these so a
failure there is a failure of the door, not of the vault. They honour the same
interface and exceptions as the real ``Board``, ``Parties`` and ``Events``.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
import time
import uuid

import httpx

from awm.board import http, stream
from awm.board.cards import Conflict, Forbidden, NotFound


def visible(party: dict, card: dict) -> bool:
    swarm = party["swarm"]
    return card["sender"]["swarm"] == swarm or card["recipient"] in (swarm, "open")


class StubParties:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def add(self, swarm: str, principal: str, relation: str):
        token = f"tok-{uuid.uuid4().hex}"
        row = {
            "party_id": uuid.uuid4().hex,
            "swarm": swarm,
            "principal": principal,
            "relation": relation,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "revoked": False,
        }
        self.rows[row["party_id"]] = row
        return dict(row), token

    def revoke(self, party_id: str) -> dict:
        if party_id not in self.rows:
            raise NotFound(party_id)
        self.rows[party_id]["revoked"] = True
        return dict(self.rows[party_id])

    def resolve(self, token):
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        for row in self.rows.values():
            if row["token_hash"] == digest and not row["revoked"]:
                return dict(row)
        return None

    def list(self) -> list[dict]:
        return [dict(r) for r in self.rows.values()]


class StubEvents:
    def __init__(self) -> None:
        self.log: list[dict] = []
        self._lock = threading.Lock()

    def append(self, type: str, card: dict) -> int:
        with self._lock:
            event = {"id": len(self.log) + 1, "type": type, "card": dict(card), "created_at": time.time()}
            self.log.append(event)
            return event["id"]

    def since(self, last_id: int, party: dict) -> list[dict]:
        with self._lock:
            return [e for e in self.log if e["id"] > (last_id or 0) and visible(party, e["card"])]

    def latest_id(self) -> int:
        with self._lock:
            return len(self.log)

    def prune(self, days: float = 30) -> int:
        return 0


class StubBoard:
    """The card rules, minimally: enough that the door's mapping is what is under test."""

    def __init__(self, events: StubEvents, claim_delay: float = 0.0) -> None:
        self.events = events
        self.cards: dict[str, dict] = {}
        self.claim_delay = claim_delay
        self.sweeps = 0
        self.last_post_party: dict | None = None
        self._lock = threading.Lock()

    def post(self, party, *, kind, recipient, title, body, priority="normal", reply_to=None):
        if kind not in ("request", "message"):
            raise ValueError(f"bad kind {kind!r}")
        self.last_post_party = party
        card = {
            "id": uuid.uuid4().hex,  # the real store mints 32 hex digits
            "kind": kind,
            "sender": {"swarm": party["swarm"], "principal": party["principal"], "party": party["party_id"]},
            "recipient": recipient,
            "claimant": None,
            "priority": priority,
            "status": "posted",
            "title": title,
            "body": body,
            "reply_to": reply_to,
            "result": None,
        }
        with self._lock:
            self.cards[card["id"]] = card
        self.events.append("card.posted", card)
        return dict(card)

    def _seen(self, party, card_id):
        card = self.cards.get(card_id)
        if card is None or not visible(party, card):
            raise NotFound(card_id)
        return card

    def claim(self, party, card_id):
        with self._lock:
            card = self._seen(party, card_id)
            if card["recipient"] not in (party["swarm"], "open"):
                raise Forbidden("not addressed to this swarm")
            if card["status"] != "posted":
                raise Conflict("the card is already held")
            time.sleep(self.claim_delay)
            card.update(claimant=party["swarm"], status="in_progress")
            snapshot = dict(card)
        self.events.append("card.claimed", snapshot)
        return snapshot

    def _finish(self, party, card_id, text, status, event):
        with self._lock:
            card = self._seen(party, card_id)
            if card["claimant"] != party["swarm"]:
                raise Forbidden("only the claimant finishes a card")
            card.update(status=status, result=text)
            snapshot = dict(card)
        self.events.append(event, snapshot)
        return snapshot

    def complete(self, party, card_id, result):
        return self._finish(party, card_id, result, "done", "card.completed")

    def fail(self, party, card_id, reason):
        return self._finish(party, card_id, reason, "failed", "card.failed")

    def get(self, party, card_id):
        with self._lock:
            return dict(self._seen(party, card_id))

    def list(self, party, **filters):
        with self._lock:
            out = [dict(c) for c in self.cards.values() if visible(party, c)]
        for key in ("recipient", "claimant", "status", "kind", "reply_to"):
            if key in filters:
                out = [c for c in out if c[key] == filters[key]]
        if "sender" in filters:
            out = [c for c in out if c["sender"]["swarm"] == filters["sender"]]
        return out

    def sweep(self):
        self.sweeps += 1
        return []


class Live:
    """The door on an ephemeral loopback port, for what ASGI test transports cannot show."""

    def __init__(self, **kwargs) -> None:
        self.parties = StubParties()
        self.events = StubEvents()
        self.board = StubBoard(self.events)
        self.wakeup = stream.Wakeup()
        self.app = http.create_app(self.board, self.parties, self.events, wakeup=self.wakeup, **kwargs)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(http.serve(self.app, "127.0.0.1", 0, sock=self.sock))
        for _ in range(100):
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(self.url + "/board/ping")
                return
            except httpx.TransportError:
                await asyncio.sleep(0.05)
        raise RuntimeError("door did not start")

    async def stop(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass
        self.sock.close()
