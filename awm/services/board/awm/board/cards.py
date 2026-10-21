"""Cards: the board's rules, over notes in the Trilium vault.

The vault is the store. A card is a note in a column folder under the board
note, and its state is the folder it sits in, so a drag in the Trilium GUI is a
real state change. The folders mirror the Swarm board: Proposed, Planned,
Active, Blocked, Pending review, Done, Rejected. This process is the only API
writer; the watcher (`Board.sweep`) notices edits made outside it and turns
them into `card.moved` events. The event log only feeds stream replay.

An archived card sits in the archive folder inside its column folder, so it
keeps the state of the column around it. In Done and Rejected that folder is
the "Older (n)" card at the end of the column. A folded card carries the
`archived` label, sits under its home card's note and takes the home's state.

Every card also carries `status`, the previous release's four-value vocabulary
derived from its state, so a peer that has not upgraded keeps reading the board.

Claim safety rests on one service-wide lock: a threading lock for threads, and
an flock on `lock_path` so a second board process refuses to start. Both are
held per path for the life of the process and shared by every `Board` built on
that path, so a supervised respawn inside one process reuses them. A claim
re-reads the card note under that lock and writes only if the card is Proposed.
The GUI does not take the lock, so a drag landing between that read and the
write is overwritten (last write wins), and a drag landing before the read makes
the claim a Conflict. `get` and `list` only read, and take no lock.

A claim is decided by state alone, because Trilium writes a note's labels one
call at a time and the move into a folder is a separate call. Every write puts
the labels first and the move last, so a failure partway leaves a Proposed card
with a stale claimant. A Proposed card is claimable whatever its claimant says,
and the claim overwrites it. A claim by the swarm that already holds the card
returns the card unchanged, so a client that timed out can retry.

Refusals use three exceptions. The door answers 404 for `NotFound` and
`Forbidden` and 409 for `Conflict`. `Conflict` means only that another swarm
holds the card, or that it is already finished. A message card is never
claimable: claiming one raises `Forbidden`, after the visibility and recipient
checks.

A reply is a message card that sets `reply_to`, as on the previous release,
and also a message on the thread of the card at the root of its chain. The
card keeps the old wire working: a peer on that release finds replies by
listing cards, and its front door takes only Proposed message cards. A reply
to a swarm in `thread_swarms` reads its replies on the thread, so its card is
filed straight into Done.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .events import Events
from .names import check_project, clean_agent, clean_node, owner_label
from .parties import OPEN, Conflict, Forbidden, NotFound, can_see
from .states import (HELD, SETTABLE, STATES, STATUS_OF, TERMINAL, from_label,
                     from_recorded, states_for)
from .vault import MESSAGE_SEQ, Vault, VaultError

__all__ = ["Board", "BoardLocked", "Conflict", "Forbidden", "NotFound"]

log = logging.getLogger("awm.board")

KINDS = ("request", "message")
PRIORITIES = ("urgent", "normal", "low")

RESULT_MARKER = "\n\n## Result\n\n"
_MAX_TEXT = 200_000

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

#: The card label holding its newest message's sequence, so a client can tell
#: it missed messages without listing them.
LAST_MESSAGE = "cardLastMsg"
#: On a reply card, the root card whose thread already holds the reply.
THREADED = "cardThreaded"
#: Width of the zero-padded `msgSeq` label, so Trilium's text sort is sequence order.
SEQ_WIDTH = 8
#: A note a person is still typing is not sealed until it has sat this long unchanged.
SETTLE_S = 1.5
#: Message notes each sweep checks against the record. The check cycles through all of them.
#: A note that differs costs one more write, so this also bounds a format change's rewrites per sweep.
VERIFY_BATCH = 10
#: A Done or Rejected card is archived this long after it entered that state.
ARCHIVE_AFTER_S = 14 * 86400.0
#: How often the sweep recounts the Older folders when it folded nothing, to catch a delete by hand.
COUNT_OLDER_S = 60.0

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BoardLocked(RuntimeError):
    """Another process holds the lock file, so a second writer would race it."""


class _Hold:
    """The process's claim on one lock path, shared by every Board built on it."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.guard = threading.RLock()
        self.refs = 1


_HOLDS: dict[str, _Hold] = {}
_HOLDS_LOCK = threading.Lock()


def _acquire(path: Path) -> tuple[str, _Hold]:
    key = os.path.realpath(path)
    with _HOLDS_LOCK:
        hold = _HOLDS.get(key)
        if hold is not None:
            hold.refs += 1
            return key, hold
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise BoardLocked(f"another process holds {path}") from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        hold = _HOLDS[key] = _Hold(fd)
        return key, hold


def _release(key: str) -> None:
    with _HOLDS_LOCK:
        hold = _HOLDS.get(key)
        if hold is None:
            return
        hold.refs -= 1
        if hold.refs <= 0:
            del _HOLDS[key]
            os.close(hold.fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _split_content(content: str | None) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    body, marker, result = content.partition(RESULT_MARKER)
    return body, (result if marker else None)


def _seq(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _public(message: dict) -> dict:
    """A message as callers see it, without the board's mirror bookkeeping."""
    return {k: v for k, v in message.items() if k != "note_id"}


def _author(message: dict) -> str:
    """Who wrote a message, as a reader of the thread should see it.

    An agent is named by where it ran (``<swarm>/<node>/<agent>``), because its
    principal is the swarm's shared one. A person typing in the Trilium GUI is
    the host operator's principal and the one party that can; anyone else
    posted through the API.
    """
    sender, agent = message["sender"], message.get("agent")
    if agent:
        return owner_label(sender["swarm"], message.get("node"), agent)
    if message["origin"] == "gui":
        return f"{sender['principal']} (Trilium)"
    return f"{sender['principal']}@{sender['swarm']}"


def _mirror_fields(message: dict) -> tuple[str, dict[str, str]]:
    """The title and labels a message's note carries, all derived from its row."""
    author = _author(message)
    labels = {MESSAGE_SEQ: str(message["seq"]).zfill(SEQ_WIDTH), "msgAt": message["at"],
              "msgFrom": author, "readOnly": ""}
    if message.get("agent"):
        labels["msgAgent"] = message["agent"]
    if message.get("node"):
        labels["msgNode"] = message["node"]
    return f"{message['seq']} · {author}", labels


def _age_s(utc_modified: str) -> float:
    """Seconds since a Trilium ``utcDateModified`` stamp. An unreadable stamp counts as new."""
    try:
        then = datetime.strptime(utc_modified.rstrip("Z"), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - then.replace(tzinfo=timezone.utc)).total_seconds()


def _iso_age_s(stamp: str | None) -> float:
    """Seconds since one of the board's own ISO stamps. An unreadable one counts as new."""
    try:
        then = datetime.fromisoformat(stamp or "")
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


class Board:
    """The card rules. At most one process holds the lock file."""

    def __init__(self, vault: Vault, events: Events, *, lock_path: str | Path,
                 gui_party: dict | None = None, thread_swarms: frozenset[str] = frozenset()):
        self._lock_key, hold = _acquire(Path(lock_path))
        self._guard = hold.guard
        self.vault = vault
        self.events = events
        #: The author of a message a person types in the Trilium GUI. Only the
        #: host's operator can open the vault, and it names no other user. It
        #: is also the board's admin: its swarm may reject any card it sees.
        #: None leaves such notes unsealed.
        self.gui_party = gui_party
        #: Swarms whose release reads replies on the thread. A reply to one is
        #: filed in Done instead of Proposed.
        self.thread_swarms = frozenset(thread_swarms)
        self.archive_after_s = ARCHIVE_AFTER_S
        #: card id to the {status, recipient, claimant} last recorded for it.
        #: `status` holds a state; rows an older release wrote hold a status.
        self._seen: dict[str, dict] = {
            card_id: {**seen, "status": from_recorded(seen["status"])}
            for card_id, seen in events.seen_all().items()}
        self._verify_cursor = 0
        self._counted_at = 0.0

    def close(self) -> None:
        """Let go of the lock file. The process exiting does the same."""
        key, self._lock_key = self._lock_key, None
        if key is not None:
            _release(key)

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- notes to cards --------------------------------------------------------

    def _card(self, rec: dict) -> dict:
        labels = rec["labels"]
        card_id = labels["cardId"]
        body, result = _split_content(rec["content"])
        reply_to = None
        target = rec["relations"].get("replyTo")
        if target:
            try:
                reply_to = self.vault.card_id_for_note(target)
            except VaultError:
                reply_to = None
        # A card still outside the folders reports the state its old column names,
        # or the one last known for it when the column is not one the board wrote.
        state = (rec.get("state") or from_label(labels.get("status"))
                 or self._seen.get(card_id, {}).get("status") or "proposed")
        return {
            "id": card_id,
            "kind": labels.get("cardKind") or "request",
            "sender": {"swarm": labels.get("cardFrom") or "",
                       "principal": labels.get("cardPrincipal") or "",
                       "party": labels.get("cardParty") or ""},
            "recipient": labels.get("cardTo") or "",
            "claimant": labels.get("cardClaimant") or None,
            "priority": labels.get("cardPriority") or "normal",
            "state": state,
            "status": STATUS_OF[state],
            "title": rec["title"],
            "body": body,
            "reply_to": reply_to,
            "result": result,
            "agent": labels.get("cardAgent") or None,
            "node": labels.get("cardNode") or None,
            "project": labels.get("project") or None,
            "owner": labels.get("owner") or None,
            "note_id": rec.get("note_id"),
            "created_at": labels.get("cardCreated") or "",
            "updated_at": labels.get("cardUpdated") or "",
            "last_message": _seq(labels.get(LAST_MESSAGE)),
        }

    def _remember(self, card: dict) -> None:
        state = {"status": card["state"], "recipient": card["recipient"],
                 "claimant": card["claimant"] or ""}
        self._seen[card["id"]] = state
        self.events.seen_set(card["id"], **state)

    def _live(self, party: dict) -> dict:
        if not isinstance(party, dict) or not party.get("swarm") or party.get("revoked"):
            raise Forbidden("no live party")
        return party

    def _is_admin(self, party: dict) -> bool:
        return bool(self.gui_party) and party["swarm"] == self.gui_party["swarm"]

    def _fetch(self, party: dict, card_id: str) -> tuple[dict, dict]:
        """The card's fresh record and card dict, or NotFound if the party may not see it."""
        rec = self.vault.read(card_id)
        if rec is None:
            raise NotFound(card_id)
        card = self._card(rec)
        if not can_see(party, card):
            raise NotFound(card_id)
        return rec, card

    def _transition(self, rec: dict, card: dict, state: str, event: str, *,
                    labels: dict[str, str] | None = None, content: str | None = None,
                    **fields) -> None:
        """Move a card to ``state`` and log ``event``.

        The labels are written first and the move last, so a failure partway
        leaves the card in the state it was in.
        """
        now = _now()
        self.vault.update(rec["note_id"], content=content, labels={
            **(labels or {}), "cardUpdated": now, "cardStateAt": now})
        self.vault.place(rec, state)
        card.update(state=state, status=STATUS_OF[state], updated_at=now, **fields)
        self.events.append(event, card)
        self._remember(card)

    # -- verbs -----------------------------------------------------------------

    def post(self, party: dict, *, kind: str, recipient: str, title: str, body: str,
             priority: str = "normal", reply_to: str | None = None,
             project: str | None = None, agent: str | None = None,
             node: str | None = None) -> dict:
        """Post a card to Proposed. A reply also posts a message on its root card's thread."""
        self._live(party)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}: {kind!r}")
        if reply_to and kind != "message":
            raise ValueError("a reply is a message card: kind must be 'message' with reply_to")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}: {priority!r}")
        if not isinstance(recipient, str) or (
                recipient != OPEN and not _SLUG.match(recipient)):
            raise ValueError(f"recipient must be a swarm slug or {OPEN!r}: {recipient!r}")
        if not isinstance(title, str):
            raise ValueError("title must be text")
        title = title.strip()
        if not title or len(title) > 300:
            raise ValueError("title must hold 1 to 300 characters")
        if not isinstance(body, str) or len(body) > _MAX_TEXT:
            raise ValueError("body must be text of at most %d characters" % _MAX_TEXT)
        if "## Result" in body:
            raise ValueError("body may not hold a '## Result' heading: the board uses it "
                             "to tell the body from the result")
        project, agent, node = check_project(project), clean_agent(agent), clean_node(node)
        with self._guard:
            relations: dict[str, str] = {}
            root_rec = root = None
            if reply_to:
                parent_rec, parent = self._fetch(party, reply_to)
                relations["replyTo"] = parent_rec["note_id"]
                root_rec, root = self._root_of(parent_rec, parent)
                if not can_see(party, root):
                    root_rec = root = None
            now = _now()
            labels = {
                "cardId": uuid.uuid4().hex,
                "cardKind": kind,
                "cardFrom": party["swarm"],
                "cardPrincipal": party["principal"],
                "cardParty": party["party_id"],
                "cardTo": recipient,
                "cardClaimant": "",
                "cardPriority": priority,
                "cardCreated": now,
                "cardUpdated": now,
                "cardStateAt": now,
            }
            if agent:
                labels["cardAgent"] = agent
            if node:
                labels["cardNode"] = node
            if project:
                labels["project"] = project
            if root is not None:
                labels[THREADED] = root["id"]
            state = "done" if root is not None and recipient in self.thread_swarms else "proposed"
            rec = self.vault.create(title, body, labels, relations, state)
            card = self._card(rec)
            self.events.append("card.posted", card)
            self._remember(card)
            if root is None:
                return card
            sender = {"swarm": party["swarm"], "principal": party["principal"],
                      "party": party["party_id"]}
            message = self._add_message(root_rec, root, sender, "api",
                                        body.strip() and body or title, agent=agent, node=node)
            return {**card, "folded_into": root["id"], "message": message}

    def _root_of(self, rec: dict, card: dict) -> tuple[dict, dict]:
        """The record and card at the root of a `reply_to` chain. A broken link ends the walk."""
        seen = {card["id"]}
        while card["reply_to"] and card["reply_to"] not in seen:
            parent = self.vault.read(card["reply_to"])
            if parent is None:
                break
            rec, card = parent, self._card(parent)
            seen.add(card["id"])
        return rec, card

    def claim(self, party: dict, card_id: str, *, agent: str | None = None,
              node: str | None = None) -> dict:
        """Claim a Proposed request: it moves to Planned and its owner is the claiming agent."""
        self._live(party)
        owner = owner_label(party["swarm"], clean_node(node), clean_agent(agent))
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if card["recipient"] not in (party["swarm"], OPEN):
                raise Forbidden("the card is not addressed to this swarm")
            if card["kind"] == "message":
                raise Forbidden("a message cannot be claimed")
            if card["state"] in HELD and card["claimant"] == party["swarm"]:
                # A retry after a lost reply. If the first attempt wrote the card
                # but died before logging it, the log still owes the claim event.
                if self._seen.get(card["id"], {}).get("status") not in HELD:
                    self.events.append("card.claimed", card)
                    self._remember(card)
                return card
            if card["state"] != "proposed":
                raise Conflict("the card is already held")
            self._transition(rec, card, "planned", "card.claimed",
                             labels={"cardClaimant": party["swarm"], "owner": owner},
                             claimant=party["swarm"], owner=owner)
            return card

    def complete(self, party: dict, card_id: str, result) -> dict:
        return self._finish(party, card_id, result, "done", "card.completed")

    def fail(self, party: dict, card_id: str, reason) -> dict:
        return self._finish(party, card_id, reason, "rejected", "card.failed")

    def _finish(self, party: dict, card_id: str, result, state: str, event: str) -> dict:
        self._live(party)
        text = result if isinstance(result, str) else json.dumps(result)
        if len(text) > _MAX_TEXT:
            raise ValueError("result is too long")
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if card["kind"] == "message" and card["recipient"] == party["swarm"]:
                # Nobody claims a message: its recipient reads it, replies and finishes it.
                if card["state"] in ("done", "rejected"):
                    raise Forbidden("the card is finished")
            elif card["claimant"] != party["swarm"]:
                raise Forbidden("only the claimant finishes a card")
            elif card["state"] not in HELD:
                raise Forbidden("the card is not in progress")
            self._transition(rec, card, state, event,
                             content=(card["body"] or "") + RESULT_MARKER + text, result=text)
            return card

    def set_status(self, party: dict, card_id: str, state: str) -> dict:
        """Move a card its claimant holds among Planned, Active, Blocked, Pending review and Done."""
        self._live(party)
        if state not in SETTABLE:
            raise ValueError(f"state must be one of {SETTABLE}: {state!r}")
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if card["claimant"] != party["swarm"]:
                raise Forbidden("only the claimant moves a card")
            if card["state"] not in HELD:
                raise Forbidden("the card is not held")
            if card["state"] != state:
                self._transition(rec, card, state, "card.moved")
            return card

    def reject(self, party: dict, card_id: str, reason, *, agent: str | None = None,
               node: str | None = None) -> dict:
        """Move an unfinished card to Rejected, with the reason as a message from the party.

        The recipient swarm, the claimant and the board's admin may reject.
        """
        self._live(party)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        if len(reason) > _MAX_TEXT:
            raise ValueError("reason must be text of at most %d characters" % _MAX_TEXT)
        agent, node = clean_agent(agent), clean_node(node)
        with self._guard:
            rec, card = self._fetch(party, card_id)
            if party["swarm"] not in (card["recipient"], card["claimant"]) \
                    and not self._is_admin(party):
                raise Forbidden("only the recipient, the claimant or the admin rejects a card")
            if card["state"] in TERMINAL:
                raise Conflict("the card is already finished")
            self._transition(rec, card, "rejected", "card.moved")
            sender = {"swarm": party["swarm"], "principal": party["principal"],
                      "party": party["party_id"]}
            message = self._add_message(rec, card, sender, "api", reason, agent=agent, node=node)
            return {**card, "message": message}

    def get(self, party: dict, card_id: str) -> dict:
        self._live(party)
        _, card = self._fetch(party, card_id)
        return card

    _FILTERS = ("sender", "recipient", "claimant", "status", "state", "kind", "reply_to",
                "project")

    def list(self, party: dict, *, limit: int | None = None, offset: int | None = None,
             **filters) -> list[dict]:
        """Cards the party may see, oldest first, filtered, then paged by ``limit`` and ``offset``.

        ``status`` takes the previous release's values and ``state`` the new
        ones, and each also takes the other's spelling.
        """
        self._live(party)
        unknown = set(filters) - set(self._FILTERS)
        if unknown:
            raise ValueError(f"unknown filter {sorted(unknown)}")
        wanted = {k: v for k, v in filters.items() if v not in (None, "")}
        states = set(STATES)
        for key in ("status", "state"):
            if key in wanted:
                states &= set(states_for(wanted[key]))
        limit = DEFAULT_LIST_LIMIT if limit is None else int(limit)
        offset = 0 if offset is None else int(offset)
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ValueError(f"limit must be 1 to {MAX_LIST_LIMIT} and offset 0 or more")
        matches = []
        for rec in self.vault.list():
            card = self._card({**rec, "content": None})
            if not can_see(party, card) or card["state"] not in states:
                continue
            if any(card[k] != wanted[k] for k in ("recipient", "claimant", "kind",
                                                  "reply_to", "project") if k in wanted):
                continue
            if "sender" in wanted and card["sender"]["swarm"] != wanted["sender"]:
                continue
            matches.append(card)
        matches.sort(key=lambda c: (c["created_at"], c["id"]))
        page = []
        for card in matches[offset:offset + limit]:
            full = self.vault.read(card["id"])
            page.append(self._card(full) if full is not None else card)
        return page

    def card_events(self, party: dict, card_id: str, *, since: int | None = None,
                    limit: int | None = None) -> list[dict]:
        """The card's events after id ``since`` that the party may see, oldest first, from the replay store."""
        self._live(party)
        limit = DEFAULT_LIST_LIMIT if limit is None else int(limit)
        since = 0 if since is None else int(since)
        if not 1 <= limit <= MAX_LIST_LIMIT or since < 0:
            raise ValueError(f"limit must be 1 to {MAX_LIST_LIMIT} and since 0 or more")
        self._fetch(party, card_id)
        return self.events.for_card(card_id, party, since, limit)

    # -- a card's thread -------------------------------------------------------

    def message(self, party: dict, card_id: str, body: str, *, agent: str | None = None,
                node: str | None = None) -> dict:
        """Add a message to the card's thread. Anyone who can see the card may post."""
        self._live(party)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body must be non-empty text")
        if len(body) > _MAX_TEXT:
            raise ValueError("body must be text of at most %d characters" % _MAX_TEXT)
        with self._guard:
            rec, card = self._fetch(party, card_id)
            sender = {"swarm": party["swarm"], "principal": party["principal"],
                      "party": party["party_id"]}
            return self._add_message(rec, card, sender, "api", body, agent=clean_agent(agent),
                                     node=clean_node(node))

    def messages(self, party: dict, card_id: str, *, after: int | None = None,
                 limit: int | None = None) -> list[dict]:
        """The card's messages after sequence ``after``, oldest first, from the board's record."""
        self._live(party)
        limit = DEFAULT_LIST_LIMIT if limit is None else int(limit)
        after = 0 if after is None else int(after)
        if not 1 <= limit <= MAX_LIST_LIMIT or after < 0:
            raise ValueError(f"limit must be 1 to {MAX_LIST_LIMIT} and after 0 or more")
        self._fetch(party, card_id)
        return [_public(m) for m in self.events.messages(card_id, after, limit)]

    def _add_message(self, rec: dict, card: dict, sender: dict, origin: str, body: str,
                     note_id: str | None = None, agent: str | None = None,
                     node: str | None = None) -> dict:
        """Record a message, mirror it into the vault, and log `card.message`.

        The row is written first and is the message. A mirror or label write
        that fails leaves a row the sweep repairs, so the post still succeeds.
        A ``note_id`` is a note a person already made, which is stamped in place.
        """
        message = self.events.message_add(card["id"], at=_now(), sender=sender,
                                          origin=origin, body=body, note_id=note_id,
                                          agent=agent, node=node)
        try:
            self._mirror(rec["note_id"], message)
            self.vault.update(rec["note_id"], labels={LAST_MESSAGE: str(message["seq"])})
        except VaultError as e:
            log.warning("message %s on card %s is recorded but not mirrored: %s",
                        message["seq"], card["id"], e)
        public = _public(message)
        card["last_message"] = message["seq"]
        self.events.append("card.message", {**card, "message": public})
        return public

    def _mirror(self, card_note: str, message: dict) -> None:
        """Write the message's note from its row: make it, or put it back as recorded."""
        title, labels = _mirror_fields(message)
        if message["note_id"]:
            self.vault.restore(message["note_id"], title, message["body"], labels)
            return
        chat = self.vault.ensure_chat(card_note)
        note_id = self.vault.create_child(chat, title, message["body"], labels)
        self.events.message_set_note(message["card_id"], message["seq"], note_id)
        message["note_id"] = note_id

    # -- the watcher -----------------------------------------------------------

    def sweep(self) -> list[dict]:
        """Notice cards whose state, recipient or claimant changed outside this process.

        Compares each card note with the state last recorded for it and emits
        `card.moved` with the new card. A card seen for the first time is
        recorded without an event. A drag back to Proposed releases the claim,
        because a card nobody holds should be claimable again. A Done or
        Rejected card is archived once it has sat there for `archive_after_s`.
        Returns the events it emitted.
        """
        moved: list[dict] = []
        with self._guard:
            self.vault.refresh()
            live = set()
            folded = False
            for rec in self.vault.list():
                card_id = rec["labels"]["cardId"]
                live.add(card_id)
                card = self._card({**rec, "content": None})
                folded = self._archive_if_old(rec, card) or folded
                previous = self._seen.get(card_id)
                if previous is None:
                    self._remember(card)
                    continue
                now = {"status": card["state"], "recipient": card["recipient"],
                       "claimant": card["claimant"] or ""}
                if previous.get("recipient") is None:    # recorded by an older schema
                    self._remember(card)
                    if now["status"] == previous["status"]:
                        continue
                elif now == previous:
                    continue
                try:
                    moved.append(self._record_move(card_id, previous["status"]))
                except VaultError as e:
                    log.warning("could not record the move of card %s: %s", card_id, e)
            stale = [c for c in self._seen if c not in live]
            for card_id in stale:
                del self._seen[card_id]
            self.events.seen_drop(stale)
            if folded or stale or time.monotonic() - self._counted_at >= COUNT_OLDER_S:
                try:
                    self.vault.count_older()
                    self._counted_at = time.monotonic()
                except VaultError as e:
                    log.warning("could not count the Older folders: %s", e)
            try:
                self._verify_messages()
            except VaultError as e:
                log.warning("message sweep stopped early: %s", e)
        return moved

    def _archive_if_old(self, rec: dict, card: dict) -> bool:
        """Fold a card that sat in Done or Rejected for `archive_after_s` into its Older folder. True if it moved."""
        if card["state"] not in TERMINAL or rec["archived_in"]:
            return False
        since = rec["labels"].get("cardStateAt") or rec["labels"].get("cardUpdated")
        if _iso_age_s(since) < self.archive_after_s:
            return False
        try:
            self.vault.archive(rec, card["state"])
        except VaultError as e:
            log.warning("could not archive card %s: %s", card["id"], e)
            return False
        return True

    def seal(self) -> None:
        """Seal the notes people added in the GUI. One search when there are none, so a caller can run it every second."""
        with self._guard:
            try:
                self._seal_gui_messages()
            except VaultError as e:
                log.warning("sealing stopped early: %s", e)

    def _seal_gui_messages(self) -> None:
        """Turn each settled note a person added under a card's chat into the card's next message."""
        if not self.gui_party:
            return
        for note in self.vault.unsealed_messages():
            note_id = note["noteId"]
            if _age_s(note.get("utcDateModified") or "") < SETTLE_S:
                continue
            card_note = next(filter(None, map(self.vault.card_note_of_chat,
                                              note.get("parentNoteIds") or [])), None)
            card_id = card_note and self.vault.card_id_for_note(card_note)
            if not card_id or self.events.message_by_note(note_id):
                continue
            rec = self.vault.read(card_id)
            found = self.vault.message_note(note_id)
            if rec is None or found is None or not found["content"].strip():
                continue
            card = self._card(rec)
            if not can_see(self.gui_party, card):
                log.warning("note %s is on card %s, which %s cannot see: left unsealed",
                            note_id, card_id, self.gui_party["swarm"])
                continue
            sender = {"swarm": self.gui_party["swarm"], "principal": self.gui_party["principal"],
                      "party": self.gui_party["party_id"]}
            self._add_message(rec, card, sender, "gui", found["content"], note_id=note_id)

    def _verify_messages(self) -> None:
        """Put the next batch of message notes back as the record has them, and mirror unmirrored ones."""
        page, self._verify_cursor = self.events.message_page(self._verify_cursor, VERIFY_BATCH)
        for message in page:
            title, labels = _mirror_fields(message)
            if message["note_id"]:
                found = self.vault.message_note(message["note_id"])
                if found is not None:
                    if (found["title"], found["labels"]) == (title, {**found["labels"], **labels}) \
                            and found["content"] == message["body"]:
                        continue
                    self.vault.restore(message["note_id"], title, message["body"], labels)
                    continue
                message["note_id"] = None
            rec = self.vault.read(message["card_id"], content=False)
            if rec is not None:
                self._mirror(rec["note_id"], message)

    def _record_move(self, card_id: str, previous_state: str) -> dict:
        rec = self.vault.read(card_id)
        if rec is None:
            raise VaultError(f"card {card_id} vanished mid-sweep")
        card = self._card(rec)
        now = _now()
        labels = {"cardUpdated": now}
        if card["state"] != previous_state:
            labels["cardStateAt"] = now
        if card["state"] == "proposed" and previous_state != "proposed" and card["claimant"]:
            labels.update(cardClaimant="", owner="")
            card.update(claimant=None, owner=None)
        self.vault.update(rec["note_id"], labels=labels)
        card["updated_at"] = now
        event_id = self.events.append("card.moved", card)
        self._remember(card)
        return {"id": event_id, "type": "card.moved", "card": card}

    def watch(self, stop: threading.Event, interval: float = 5.0) -> None:
        """Run `sweep` every ``interval`` seconds until ``stop`` is set, pruning old events hourly."""
        pruned = 0.0
        while not stop.is_set():
            try:
                self.sweep()
                self.seal()
                if pruned <= 0:
                    self.events.prune()
                    pruned = 3600.0
            except Exception:
                log.exception("board sweep failed")
            pruned -= interval
            stop.wait(interval)

    # -- migrations ------------------------------------------------------------

    def migrate_columns(self, dry_run: bool = True) -> dict:
        """Move every card still directly under the board into the folder for its status."""
        from . import migrate
        return migrate.columns(self, dry_run)

    def migrate_archive(self, dry_run: bool = True) -> dict:
        """Give Done and Rejected their Older folders, and file every archived card. See `migrate.archive`."""
        from . import migrate
        return migrate.archive(self, dry_run)

    def migrate_homes(self, dry_run: bool = True) -> dict:
        """Move each folded card under the card it was folded into. See `migrate.homes`."""
        from . import migrate
        return migrate.homes(self, dry_run)

    def migrate_threads(self, dry_run: bool = True, proposal_path: str | Path | None = None) -> dict:
        """Fold reply cards into the threads of their root cards. See `migrate.threads`."""
        from . import migrate
        return migrate.threads(self, dry_run, proposal_path)
