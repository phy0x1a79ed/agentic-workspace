"""This node's view of the federation board, through the local gateway's ``board`` verbs.

The board service on this node relays to the host with the swarm's token, so
the same route works on every node and no token reaches this service. A call
made for a session carries the session's pid: the board stamps the agent's name
and the node from it, as it does for the session's own calls.
"""

from __future__ import annotations

import re
from typing import Any

from awm import gatewayclient

from awm.reporting.intake import short_ref

PAGE = 500
MAX_PAGES = 20
#: The shortest card id prefix a reference may use.
MIN_PREFIX = 6

_HEX = re.compile(r"[0-9a-f]+")
_NOTE_URL = re.compile(r"https?://[^\s#]+#(?:[\w-]+/)*([\w-]{4,})")


class FedError(RuntimeError):
    """The board refused a call. ``status`` is 0 when it could not be reached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status >= 500


class AmbiguousRef(ValueError):
    """A short card id matches more than one card."""


class Federation:
    async def _call(self, fn: str, args: dict, caller_pid: int | None = None) -> dict:
        try:
            reply = await gatewayclient.call("board", fn, args, caller_pid=caller_pid, timeout=60.0)
        except Exception as exc:  # noqa: BLE001 — every transport failure is the same to a caller
            raise FedError(0, f"the board could not be reached: {exc}") from exc
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            reply = reply if isinstance(reply, dict) else {}
            raise FedError(int(reply.get("status") or 0), str(reply.get("error") or reply or "refused"))
        return reply

    async def get(self, card_id: str) -> dict:
        return (await self._call("get", {"card_id": card_id}))["card"]

    async def list(self, **filters: Any) -> list[dict]:
        return (await self._call("list", {k: v for k, v in filters.items() if v}))["cards"]

    async def cards(self) -> list[dict]:
        """Every card this swarm may see, paged."""
        out: list[dict] = []
        for page in range(MAX_PAGES):
            batch = await self.list(limit=PAGE, offset=page * PAGE)
            out.extend(batch)
            if len(batch) < PAGE:
                break
        return out

    async def claim(self, card_id: str, caller_pid: int | None = None) -> dict:
        return (await self._call("claim", {"card_id": card_id}, caller_pid))["card"]

    async def status(self, card_id: str, state: str) -> dict:
        return (await self._call("status", {"card_id": card_id, "state": state}))["card"]

    async def reject(self, card_id: str, reason: str, caller_pid: int | None = None) -> dict:
        return (await self._call("reject", {"card_id": card_id, "reason": reason}, caller_pid))["card"]

    async def message(self, card_id: str, body: str, caller_pid: int | None = None) -> dict:
        return (await self._call("message_post", {"card_id": card_id, "body": body}, caller_pid))["message"]

    async def resolve(self, ref: str) -> dict | None:
        """The card a reference names, or None when it names none.

        A reference is a full card id, a unique prefix of one, or the card's
        Trilium URL (or bare note id). A prefix that fits several cards raises
        ``AmbiguousRef``.
        """
        text = ref.strip()
        found = _NOTE_URL.search(text)
        token = (found.group(1) if found else text).strip()
        lowered = token.lower()
        if _HEX.fullmatch(lowered) and len(lowered) == 32:
            try:
                return await self.get(lowered)
            except FedError as exc:
                if exc.status == 404:
                    return None
                raise
        cards = None
        if _HEX.fullmatch(lowered) and len(lowered) >= MIN_PREFIX:
            cards = await self.cards()
            hits = [c for c in cards if str(c.get("id", "")).startswith(lowered)]
            if len(hits) > 1:
                raise AmbiguousRef(f"{lowered} matches {len(hits)} cards: "
                                   + ", ".join(short_ref(c["id"]) + "…" for c in hits[:5])
                                   + "; use more of the id")
            if hits:
                return hits[0]
        if not token or re.search(r"\s", token):
            return None
        cards = cards if cards is not None else await self.cards()
        return next((c for c in cards if c.get("note_id") == token), None)
