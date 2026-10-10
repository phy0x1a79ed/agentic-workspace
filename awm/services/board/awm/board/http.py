"""The board's HTTP door: a bearer token in, a card verb out.

Every refusal answers 404 with the same body — a bad or revoked token, a card
the caller may not see, a card that does not exist, a path that is not a route.
A caller cannot tell them apart, so the door confirms nothing about what is
behind it. The one exception to "refusal means 404" is a claim on a card
somebody holds, which answers 409 because the caller has already proved it may
see the card.

The vault is already written by the time ``Board`` returns, so the door does
nothing after a mutating call.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from awm.board import HEARTBEAT_S, PREFIX
from awm.board import stream as streams

log = logging.getLogger("awm.board.http")

LIST_FILTERS = ("sender", "recipient", "claimant", "status", "kind", "reply_to")

_NOT_FOUND = {"error": "not found"}


def not_found() -> JSONResponse:
    return JSONResponse(_NOT_FOUND, status_code=404)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def _exceptions() -> tuple[type, type, type, type]:
    """The board's refusal types, imported late so this module loads without the store."""
    from awm.board.cards import Conflict, Forbidden, NotFound
    from awm.board.vault import VaultError

    return NotFound, Forbidden, Conflict, VaultError


class Door:
    """The routes, bound to one board, one party table and one event log."""

    def __init__(
        self,
        board: Any,
        parties: Any,
        events: Any,
        *,
        wakeup: streams.Wakeup | None = None,
        heartbeat_s: float = HEARTBEAT_S,
        poll_s: float = streams.POLL_S,
    ) -> None:
        self.board = board
        self.parties = parties
        self.events = events
        self.wakeup = wakeup
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s

    # -- plumbing ------------------------------------------------------------

    async def _party(self, request: Request) -> dict | None:
        token = _bearer(request)
        if token is None:
            return None
        row = await asyncio.to_thread(self.parties.resolve, token)
        if not row or row.get("revoked"):
            return None
        return row

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> JSONResponse:
        """Run one store call and map its refusals onto status codes."""
        NotFound, Forbidden, Conflict, VaultError = _exceptions()
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        except (NotFound, Forbidden):
            return not_found()
        except Conflict as exc:
            return JSONResponse({"error": str(exc) or "conflict"}, status_code=409)
        except VaultError:
            log.exception("board: the vault failed")
            return JSONResponse({"error": "vault unavailable"}, status_code=503)
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": f"bad request: {exc}"}, status_code=400)
        return JSONResponse(result, status_code=200)

    @staticmethod
    async def _body(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _card_id(request: Request) -> str | None:
        """The id as the store knows it, or None when it is not a UUID in either spelling.

        The store mints 32 hex digits with no dashes; the check accepts that
        and the dashed form but passes the id through untouched.
        """
        raw = request.path_params["card_id"]
        try:
            uuid.UUID(raw)
        except ValueError:
            return None
        return raw

    # -- routes --------------------------------------------------------------

    async def post_card(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        if party is None:
            return not_found()
        body = await self._body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        missing = [k for k in ("kind", "recipient", "title") if not body.get(k)]
        if missing:
            return JSONResponse({"error": f"missing: {', '.join(missing)}"}, status_code=400)
        # Any "sender" in the body is dropped on purpose: the sender is the bearer.
        return await self._call(
            self.board.post,
            party,
            kind=body["kind"],
            recipient=body["recipient"],
            title=body["title"],
            body=body.get("body") or "",
            priority=body.get("priority") or "normal",
            reply_to=body.get("reply_to") or None,
        )

    async def list_cards(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        if party is None:
            return not_found()
        filters = {k: v for k in LIST_FILTERS if (v := request.query_params.get(k))}
        return await self._call(self.board.list, party, **filters)

    async def get_card(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        card_id = self._card_id(request)
        if party is None or card_id is None:
            return not_found()
        return await self._call(self.board.get, party, card_id)

    async def claim_card(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        card_id = self._card_id(request)
        if party is None or card_id is None:
            return not_found()
        return await self._call(self.board.claim, party, card_id)

    async def complete_card(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        card_id = self._card_id(request)
        if party is None or card_id is None:
            return not_found()
        body = await self._body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        return await self._call(self.board.complete, party, card_id, body.get("result"))

    async def fail_card(self, request: Request) -> JSONResponse:
        party = await self._party(request)
        card_id = self._card_id(request)
        if party is None or card_id is None:
            return not_found()
        body = await self._body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        return await self._call(self.board.fail, party, card_id, body.get("reason"))

    async def stream(self, request: Request) -> StreamingResponse | JSONResponse:
        party = await self._party(request)
        if party is None:
            return not_found()
        last_id = streams.parse_last_event_id(
            request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        )
        token = _bearer(request)

        def still_valid() -> bool:
            row = self.parties.resolve(token)
            return bool(row) and not row.get("revoked")

        body = streams.event_stream(
            self.events,
            party,
            last_id,
            wakeup=self.wakeup,
            heartbeat_s=self.heartbeat_s,
            poll_s=self.poll_s,
            still_valid=still_valid,
        )
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


async def _refuse(request: Request, exc: Exception) -> JSONResponse:
    """A wrong method, an unknown path and a 500's detail all look the same from outside."""
    if isinstance(exc, HTTPException) and exc.status_code not in (404, 405):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return not_found()


def create_app(
    board: Any,
    parties: Any,
    events: Any,
    *,
    wakeup: streams.Wakeup | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    poll_s: float = streams.POLL_S,
) -> Starlette:
    door = Door(board, parties, events, wakeup=wakeup, heartbeat_s=heartbeat_s, poll_s=poll_s)
    routes = [
        Route(f"{PREFIX}/cards", door.post_card, methods=["POST"]),
        Route(f"{PREFIX}/cards", door.list_cards, methods=["GET"]),
        Route(f"{PREFIX}/cards/{{card_id}}", door.get_card, methods=["GET"]),
        Route(f"{PREFIX}/cards/{{card_id}}/claim", door.claim_card, methods=["POST"]),
        Route(f"{PREFIX}/cards/{{card_id}}/complete", door.complete_card, methods=["POST"]),
        Route(f"{PREFIX}/cards/{{card_id}}/fail", door.fail_card, methods=["POST"]),
        Route(f"{PREFIX}/stream", door.stream, methods=["GET"]),
    ]
    return Starlette(routes=routes, exception_handlers={HTTPException: _refuse})


async def serve(app: Starlette, host: str, port: int, *, sock: Any = None) -> None:
    """Run the door until cancelled. Binds loopback only; the edge is the public side."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # the adapter owns signals
    try:
        await server.serve(sockets=[sock] if sock is not None else None)
    except asyncio.CancelledError:
        server.should_exit = True
        raise
