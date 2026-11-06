"""The host half of every room: the relay's rooms, who holds them, and what waits for each.

A room's members are sessions on member nodes. A member node's ``channel``
plugin calls this through deneb's door, naming the member; the door stamps the
node (``fleet.md`` R98). Nothing here calls out: no push, no door, no edge
(R79). A member's node holds a ``feed`` long-poll instead, which returns what
arrived after each member's cursor and renews each member's lease. A member
whose node stops renewing it lapses, and a room with no member left ends
(``channels.md`` R72 item 5).

Messages wait in the store, so a restart loses none that a member has not
fetched. In an encrypted room the store holds only sealed text and the key
exchange: the member's own node seals, opens and keeps the plaintext (R73). An
encrypted room therefore has one member.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from awm.relay.rooms import LASTS, RelayError, RoomGone, Rooms
from awm.relay.store import GUEST, MEMBER, NOTICE, Store

log = logging.getLogger("awm.relay.service")

#: How long a member stays without its node renewing it.
LEASE_S = float(os.environ.get("AWM_RELAY_LEASE_S") or 180)
PRUNE_S = 30.0
#: The longest one feed waits for something to arrive.
FEED_MAX_S = 60.0
#: How long a closed room's messages are kept for members still to fetch them.
KEEP_CLOSED_S = 7 * 24 * 3600.0

#: The keys of a message a member may send: plaintext, a sealed message, or a key exchange.
BODY_KEYS = ("text", "sealed", "n", "spake2")

#: rooms.sealed: no encryption, an offer is out, our side has sent sealed text.
PLAIN, OFFERED, SEALED = 0, 1, 2


class RelayRefused(ValueError):
    """The call cannot go ahead. The message says why."""


class Relay:
    def __init__(self, store: Store, rooms: Rooms, lease_s: float = LEASE_S) -> None:
        self.store = store
        self.rooms = rooms
        self.lease_s = lease_s
        self._following: dict[str, asyncio.Task] = {}
        self._changed = asyncio.Event()
        self._stopping = False

    # -- verbs ---------------------------------------------------------------

    async def open(self, member: dict, persistent: bool = False) -> dict:
        lasts = LASTS[1] if persistent else LASTS[0]
        try:
            name, token = await self.rooms.open(lasts)
        except httpx.HTTPError as exc:
            raise RelayRefused(f"the tether relay at {self.rooms.base} is unreachable: {exc}") from exc
        except RelayError as exc:
            raise RelayRefused(str(exc)) from exc
        row = self.store.add_room(name=name, lasts=lasts, link=self.rooms.link(name), token=token)
        self.store.add_member(name, member)
        self._follow(name)
        return self._public(row)

    async def join(self, member: dict, name: str) -> dict:
        row = self._open_room(name)
        if row["sealed"] != PLAIN:
            raise RelayRefused(f"room {name} is encrypted, and an encrypted room has one "
                               "member: its key never leaves that member's node")
        self.store.add_member(name, member)
        return self._public(row)

    async def write(self, member: dict, name: str, message: dict) -> dict:
        row = self._open_room(name)
        if not self.store.member(name, member["node"], member["session"]):
            raise RelayRefused(f"you are not a member of room {name}; `join` it first")
        body = {k: message[k] for k in BODY_KEYS if k in message}
        if not body:
            raise RelayRefused("a message carries `text`, `sealed` with `n`, or `spake2`")
        if "spake2" in body:
            if len(self.store.members(name)) > 1:
                raise RelayRefused(f"room {name} has more than one member, so it cannot be "
                                   "encrypted: its key would have to pass through this relay")
            self.store.seal(name, OFFERED)
        if "sealed" in body:
            self.store.seal(name, SEALED)
        elif "text" in body and row["sealed"] == SEALED:
            raise RelayRefused(f"room {name} is encrypted, so plaintext is refused")
        self.store.renew(member["node"], [member["session"]])
        try:
            seq = await self.rooms.send(name, row["token"], {"from": member["who"], **body})
        except RoomGone as exc:
            await self._ended(name, "the relay no longer has this room")
            raise RelayRefused(f"room {name} has ended; open a new one") from exc
        except httpx.HTTPError as exc:
            raise RelayRefused(f"the tether relay is unreachable, nothing was sent: {exc}") from exc
        except RelayError as exc:
            raise RelayRefused(str(exc)) from exc
        self.store.add_message(name, by=MEMBER, body=body, seq=seq, member=member)
        self._wake()
        return {"name": name, "sent": seq}

    async def close(self, member: dict, name: str) -> dict:
        if not self.store.leave(name, member["node"], member["session"], "it left"):
            raise RelayRefused(f"you are not a member of room {name}")
        if not self.store.members(name):
            await self._shut(name, "its last member left")
        return {"closed": name}

    def status(self, member: dict) -> dict:
        rows = self.store.of_session(member["node"], member["session"])
        out = []
        for r in rows:
            item = self._public(r)
            item.update(member_left=r["left_why"] if r["left_at"] else None,
                        members=len(self.store.members(r["name"])))
            out.append(item)
        return {"rooms": out}

    async def feed(self, node: str, live: list[str] | None, acks: list[dict],
                   wait: float = 0.0) -> dict:
        """What waits for ``node``'s members in ``live``, after their cursors.

        ``acks`` move cursors past what the node has typed. ``live`` renews those
        members' leases; ``None`` renews all of the node's members, for a node
        that cannot tell which of its sessions run.
        """
        for ack in acks:
            self.store.ack(str(ack["room"]), node, str(ack["session"]), int(ack["through"]))
        deadline = time.monotonic() + min(max(wait, 0.0), FEED_MAX_S)
        while True:
            self.store.renew(node, live)
            changed = self._changed
            messages, members = self._waiting(node, live)
            remaining = deadline - time.monotonic()
            if messages or remaining <= 0 or self._stopping:
                return {"messages": messages, "members": members}
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    # -- lifecycle -----------------------------------------------------------

    def resume(self) -> None:
        """Follow every open room again, from its saved cursor."""
        for row in self.store.open_rooms():
            self._follow(row["name"])

    async def prune(self) -> list[str]:
        """Drop every member whose lease lapsed, and end each room left with none."""
        gone: list[str] = []
        for m in self.store.lapsed(time.time() - self.lease_s):
            self.store.leave(m["room"], m["node"], m["session"], "its session ended")
            log.info("room %s: member %s@%s lapsed", m["room"], m["session"], m["node"])
            gone.append(f"{m['session']}@{m['node']}")
            row = self.store.room(m["room"])
            if row and row["open"] and not self.store.members(m["room"]):
                await self._shut(m["room"], "its last member's session ended")
        self.store.forget(time.time() - KEEP_CLOSED_S)
        return gone

    async def prune_forever(self, every_s: float = PRUNE_S) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.prune()
            except Exception as exc:  # noqa: BLE001 — retried next round
                log.warning("relay prune failed: %s", exc)

    async def stop(self) -> None:
        self._stopping = True
        self._wake()
        for task in self._following.values():
            task.cancel()
        await asyncio.gather(*self._following.values(), return_exceptions=True)
        self._following.clear()

    # -- internals -----------------------------------------------------------

    def _open_room(self, name: str) -> dict:
        row = self.store.room(name)
        if row is None:
            raise RelayRefused(f"no room is named {name}")
        if not row["open"]:
            raise RelayRefused(f"room {name} has ended: {row['ended']}")
        return row

    def _public(self, row: dict) -> dict:
        return {"name": row["name"], "lasts": row["lasts"], "link": row["link"],
                "open": row["open"], "ended": row["ended"],
                "encrypted": row["sealed"] == SEALED}

    def _waiting(self, node: str, live: list[str] | None) -> tuple[list[dict], list[dict]]:
        alive = None if live is None else set(live)
        messages: list[dict] = []
        members: list[dict] = []
        for m in self.store.of_node(node):
            if alive is not None and m["session"] not in alive:
                continue
            pending = self.store.pending(m)
            if not pending:
                row = self.store.room(m["room"])
                if row and not row["open"]:
                    self.store.leave(m["room"], node, m["session"], "the room ended")
                    continue
            members.append({"room": m["room"], "session": m["session"]})
            for msg in pending:
                messages.append({"id": msg["id"], "room": msg["room"], "session": m["session"],
                                 "seq": msg["seq"], "by": msg["by"], "who": msg["who"],
                                 "level": msg["level"], "body": msg["body"]})
        return messages, members

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _follow(self, name: str) -> None:
        if name not in self._following or self._following[name].done():
            self._following[name] = asyncio.create_task(self._follow_room(name))

    async def _follow_room(self, name: str) -> None:
        """Keep what the guest sends. Our members' messages are kept as they write them."""
        backoff = 1.0
        try:
            while not self._stopping:
                row = self.store.room(name)
                if not row or not row["open"]:
                    return
                try:
                    batch = await self.rooms.recv(name, row["token"], row["cursor"])
                except RoomGone:
                    await self._ended(name, "the relay no longer has this room")
                    return
                except httpx.HTTPError as exc:
                    # A cancel that lands inside httpx's read can surface as an
                    # httpx error instead. Retrying it would outlive `stop`.
                    if self._stopping or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError from exc
                    log.info("room %s: receive failed, retrying: %s", name, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = 1.0
                for message in batch.get("messages") or []:
                    if message.get("by") == "host":
                        continue
                    body = {k: v for k, v in message.items() if k not in ("seq", "by", "from")}
                    who = str(message.get("from") or "the guest")
                    self.store.add_message(name, by=GUEST, body=body,
                                           seq=int(message.get("seq") or 0), member={"who": who})
                self.store.set_cursor(name, int(batch.get("next") or 0))
                self._wake()
                if batch.get("ended"):
                    await self._ended(name, batch["ended"])
                    return
        finally:
            self._following.pop(name, None)

    async def _ended(self, name: str, why: str) -> None:
        """The room ended on the relay's side: tell every member."""
        if not self.store.close_room(name, why):
            return
        self.store.add_message(name, by=NOTICE, body={"text": why, "ended": True})
        self._wake()

    async def _shut(self, name: str, why: str) -> None:
        """End the room from our side."""
        row = self.store.room(name)
        if not row or not self.store.close_room(name, why):
            return
        task = self._following.pop(name, None)
        if task is not None:
            task.cancel()
        await self.rooms.leave(name, row["token"])
        self.store.add_message(name, by=NOTICE, body={"text": why, "ended": True})
        self._wake()
