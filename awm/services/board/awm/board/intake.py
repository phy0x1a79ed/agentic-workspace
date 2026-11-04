"""Intake: federation-board posts to this swarm become Proposed cards (channels.md R77).

It runs in the door's own process and reads the door's event log, so it needs no
board client and no stream. A card posted to this swarm or to ``open`` goes to
operations ``intake_post``; a message on a card's thread goes to ``intake_message``.
operations makes the card, tells Tony and tells the card's session. A card's text is
the poster's data, and intake passes it through unread.

It starts at the log's newest event the first time, so the history of the old queue
is not posted again. The cursor moves only after operations took the event.
It goes, with the door, once Caviar and muse hold persistent rooms.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm.board.parties import OPEN

log = logging.getLogger("awm.board.intake")

POLL_S = 5.0
PAGE = 200

Call = Callable[[str, dict], Awaitable[Any]]


def party_label(party: Any, agent: str | None = None) -> str:
    """``<swarm>/<principal>``, with the agent when the board stamped one."""
    if isinstance(party, dict):
        text = "/".join(p for p in (party.get("swarm"), party.get("principal")) if p) or "?"
    else:
        text = str(party or "?")
    return f"{text} ({agent})" if agent else text


class Intake:
    def __init__(self, events: Any, party: dict, call: Call, cursor_path: Path) -> None:
        self.events, self.party, self.call, self.cursor_path = events, party, call, cursor_path
        self.swarm = party["swarm"]

    def cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text())["after"])
        except (OSError, ValueError, KeyError, TypeError):
            latest = int(self.events.latest_id())
            self._save(latest)
            return latest

    def _save(self, after: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"after": after}))
        tmp.replace(self.cursor_path)

    def _request(self, event: dict) -> tuple[str, dict] | None:
        card = event["card"]
        if (card.get("sender") or {}).get("swarm") == self.swarm:
            return None
        if event["type"] == "card.posted" and card.get("recipient") in (self.swarm, OPEN):
            return "intake_post", {
                "board_card": card["id"], "sender": party_label(card.get("sender"), card.get("agent")),
                "title": card.get("title") or "", "body": card.get("body") or "",
                "reply_to": card.get("reply_to") or ""}
        message = card.get("message")
        if event["type"] == "card.message" and isinstance(message, dict) \
                and (message.get("sender") or {}).get("swarm") != self.swarm:
            return "intake_message", {
                "board_card": card["id"],
                "sender": party_label(message.get("sender"), message.get("agent")),
                "body": message.get("body") or ""}
        return None

    async def step(self) -> int:
        """Hand operations the events after the cursor. Returns how many it took."""
        after = await asyncio.to_thread(self.cursor)
        rows = await asyncio.to_thread(self.events.since, after, self.party, PAGE)
        for event in rows:
            request = self._request(event)
            if request is not None:
                out = await self.call(*request)
                if isinstance(out, dict) and out.get("ok") is False:
                    log.warning("intake: operations refused board event %s: %s", event["id"],
                                out.get("error"))
            await asyncio.to_thread(self._save, event["id"])
        return len(rows)

    async def run(self) -> None:
        while True:
            try:
                n = await self.step()
            except Exception as exc:  # noqa: BLE001 - operations may be down; the cursor waits
                log.warning("intake: %s; retrying", exc)
                n = 0
            await asyncio.sleep(0 if n >= PAGE else POLL_S)
