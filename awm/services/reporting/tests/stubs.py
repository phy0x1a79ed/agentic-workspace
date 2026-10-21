"""Stand-ins for the board, the federation gateway verbs, cx and Trilium."""

from __future__ import annotations

from datetime import datetime, timezone

from awm.board.client import BoardRefused
from awm.reporting.federation import FedError, Federation
from awm.reporting.sessions import CxUnavailable

_N = 0


def make_card(**over) -> dict:
    global _N
    _N += 1
    card = {
        "id": f"{_N:08x}" + "c0de" * 6, "kind": "request", "recipient": "tony", "status": "posted",
        "state": "proposed", "priority": "normal", "title": f"card {_N}", "body": "do the thing",
        "sender": {"swarm": "beta", "principal": "op", "party": "p"}, "agent": None,
        "claimant": None, "owner": None, "project": None, "note_id": None, "last_message": 0,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    card.update(over)
    return card


class FakeBoard:
    """Just enough of BoardClient: reads and a scripted stream. The door never claims."""

    def __init__(self, swarm: str = "tony") -> None:
        self.swarm = swarm
        self.cards: dict[str, dict] = {}
        self.events: list[tuple[int, str, dict]] = []
        self.stream_starts: list[int | None] = []
        self.list_calls: list[dict] = []
        self.list_error: Exception | None = None
        self.get_errors: dict[str, Exception] = {}
        self.threads: dict[str, list[dict]] = {}
        self.message_reads: list[tuple[str, int | None]] = []

    def add(self, card: dict) -> dict:
        self.cards[card["id"]] = card
        return card

    def say(self, card: dict, body: str = "hello", swarm: str = "beta", agent: str | None = None) -> dict:
        """Add a message to ``card``'s thread. Returns the ``card.message`` event's card."""
        thread = self.threads.setdefault(card["id"], [])
        message = {"card_id": card["id"], "seq": len(thread) + 1, "at": "2026-10-14T12:00:00Z",
                   "sender": {"swarm": swarm, "principal": "op", "party": "p"},
                   "origin": "api", "body": body, "agent": agent}
        thread.append(message)
        card["last_message"] = message["seq"]
        return {**card, "message": message}

    async def messages(self, card_id: str, *, after: int | None = None,
                       limit: int | None = None) -> list[dict]:
        self.message_reads.append((card_id, after))
        if card_id not in self.cards:
            raise BoardRefused(404, "not found")
        return [dict(m) for m in self.threads.get(card_id, []) if m["seq"] > (after or 0)]

    def post_event(self, type_: str, card: dict, event_id: int | None = None) -> int:
        event_id = event_id or (self.events[-1][0] + 1 if self.events else 1)
        self.events.append((event_id, type_, dict(card)))
        return event_id

    async def get(self, card_id: str) -> dict:
        if card_id in self.get_errors:
            raise self.get_errors[card_id]
        if card_id not in self.cards:
            raise BoardRefused(404, "not found")
        return dict(self.cards[card_id])

    async def list(self, *, limit: int | None = None, offset: int | None = None,
                   **filters) -> list[dict]:
        self.list_calls.append(dict(filters, limit=limit, offset=offset))
        if self.list_error is not None:
            raise self.list_error
        out = []
        for card in self.cards.values():
            if any(card.get(k) != v for k, v in filters.items()
                   if v and k in ("recipient", "kind", "state", "claimant")):
                continue
            if filters.get("sender") and card["sender"]["swarm"] != filters["sender"]:
                continue
            out.append(dict(card))
        start = offset or 0
        return out[start:start + limit] if limit else out[start:]

    async def stream(self, last_event_id: int | None = None, *, on_state=None):
        self.stream_starts.append(last_event_id)
        if on_state is not None:
            on_state(True)
        for event in list(self.events):
            if event[0] > (last_event_id or 0):
                yield event


class FakeFed(Federation):
    """The board's gateway verbs over an in-memory set of cards.

    ``claim`` stamps the owner from the caller pid the way the board would, so a
    test can check who it acted for. ``fail`` makes the next call to a verb
    raise, and ``calls`` records every call.
    """

    def __init__(self, swarm: str = "tony", node: str = "altair") -> None:
        self.swarm, self.node = swarm, node
        self.db: dict[str, dict] = {}
        self.calls: list[tuple[str, dict, int | None]] = []
        self.fail: dict[str, FedError] = {}
        self.sticky: dict[str, FedError] = {}
        self.thread: list[tuple[str, str, int | None]] = []
        self.names: dict[int, str] = {}

    def add(self, card: dict) -> dict:
        self.db[card["id"]] = card
        return card

    def verbs(self, fn: str) -> list[dict]:
        return [a for f, a, _ in self.calls if f == fn]

    async def _call(self, fn: str, args: dict, caller_pid: int | None = None) -> dict:
        self.calls.append((fn, dict(args), caller_pid))
        if fn in self.fail:
            raise self.fail.pop(fn)
        if fn in self.sticky:
            raise self.sticky[fn]
        return getattr(self, f"_{fn}")(args, caller_pid)

    def _card(self, args: dict) -> dict:
        card = self.db.get(args["card_id"])
        if card is None:
            raise FedError(404, "not found")
        return card

    def _get(self, args, pid):
        return {"ok": True, "card": dict(self._card(args))}

    def _list(self, args, pid):
        out = [dict(c) for c in self.db.values()
               if all(c.get(k) == v for k, v in args.items() if k in ("recipient", "state", "sender"))]
        start = int(args.get("offset") or 0)
        return {"ok": True, "cards": out[start:start + int(args.get("limit") or 500)]}

    def _claim(self, args, pid):
        card = self._card(args)
        if card["state"] != "proposed" and card.get("claimant") != self.swarm:
            raise FedError(409, "the card is already held")
        if card["state"] == "proposed":
            agent = self.names.get(pid)
            card.update(state="planned", status="in_progress", claimant=self.swarm,
                        owner="/".join([self.swarm, self.node] + ([agent] if agent else [])))
        return {"ok": True, "card": dict(card)}

    def _status(self, args, pid):
        card = self._card(args)
        if card.get("claimant") != self.swarm:
            raise FedError(403, "only the claimant moves a card")
        card.update(state=args["state"])
        return {"ok": True, "card": dict(card)}

    def _reject(self, args, pid):
        card = self._card(args)
        card.update(state="rejected", status="failed")
        self.thread.append((card["id"], args["reason"], pid))
        return {"ok": True, "card": dict(card)}

    def _message_post(self, args, pid):
        self._card(args)
        self.thread.append((args["card_id"], args["body"], pid))
        return {"ok": True, "message": {"seq": len(self.thread), "body": args["body"]}}


class FakeCx:
    """cx as the service sees it: `list` and `start`."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.started: list[dict] = []
        self.unavailable = False
        self.start_reply: dict | None = None
        self._n = 0

    async def list(self) -> list[dict]:
        if self.unavailable:
            raise CxUnavailable("cx is down")
        return [dict(r) for r in self.rows]

    async def start(self, spec: dict) -> dict:
        self.started.append(dict(spec))
        if self.start_reply is not None:
            return self.start_reply
        self._n += 1
        job = f"job{self._n:04d}"
        self.rows.append({"job": job, "name": spec["name"], "state": "idle",
                          "project": spec["project"], "scope": spec["scope"]})
        return {"ok": True, "job": job, "link": f"https://claude.ai/code/session_{job}"}


class FakeTrilium:
    """The trilium note verbs the reporting service calls, over an in-memory tree."""

    def __init__(self) -> None:
        self.notes: dict[str, dict] = {"root": {"title": "root", "parents": [], "labels": {}}}
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None
        self._n = 0

    def add(self, title: str, parent: str = "root", *, type: str = "text", content: str = "",
            labels: dict | None = None, mime: str | None = None) -> str:
        self._n += 1
        note_id = f"n{self._n:011d}"
        self.notes[note_id] = {"title": title, "parents": [parent], "type": type, "content": content,
                               "mime": mime, "labels": dict(labels or {})}
        return note_id

    def kids(self, parent: str) -> list[str]:
        return [i for i, n in self.notes.items() if parent in n["parents"]]

    def parents(self, note_id: str) -> list[str]:
        return list(self.notes[note_id]["parents"])

    async def __call__(self, fn: str, **args) -> dict:
        self.calls.append((fn, args))
        if self.fail is not None:
            raise self.fail
        return getattr(self, f"_{fn}")(**args)

    def writes(self) -> list[str]:
        return [fn for fn, _ in self.calls if fn not in ("note_get", "note_children", "note_search")]

    def _note_search(self, query: str, limit: int = 50, ancestor: str | None = None) -> dict:
        if query.startswith("#"):
            hits = [i for i, n in self.notes.items() if query[1:] in n["labels"]]
        else:
            title = query.split("=", 1)[1].strip()
            hits = [i for i, n in self.notes.items() if n["title"] == title]
        return {"results": [{"noteId": i, "title": self.notes[i]["title"],
                             "parentNoteIds": self.parents(i)} for i in hits[:limit]]}

    def _note_children(self, note_id: str) -> dict:
        return {"children": [{"note_id": i, "title": self.notes[i]["title"], "type": self.notes[i].get("type"),
                              "labels": sorted(self.notes[i]["labels"]),
                              "child_count": len(self.kids(i))} for i in self.kids(note_id)]}

    def _note_get(self, note_id: str, content: bool = True) -> dict:
        if note_id not in self.notes:
            raise RuntimeError(f"404 {note_id}")
        n = self.notes[note_id]
        out = {"noteId": note_id, "title": n["title"], "parentNoteIds": self.parents(note_id),
               "utcDateModified": n.get("modified", "2026-01-02 03:04:05.678Z"),
               "attributes": [{"name": k, "value": v, "type": "label", "noteId": note_id}
                              for k, v in n["labels"].items()]}
        if content:
            out["content"] = n.get("content", "")
        return out

    def _note_create(self, title: str, parent: str = "root", type: str = "text", content: str = "",
                     mime: str | None = None, labels: dict | None = None, relations: dict | None = None) -> dict:
        note_id = self.add(title, parent, type=type, content=content, labels={**(labels or {})}, mime=mime)
        self.notes[note_id]["relations"] = dict(relations or {})
        return {"note_id": note_id, "created": True}

    def _note_update(self, note_id: str, content: str | None = None, title: str | None = None,
                     labels: dict | None = None, type: str | None = None, mime: str | None = None) -> dict:
        n = self.notes[note_id]
        if content is not None:
            n["content"] = content
        if title is not None:
            n["title"] = title
        if type is not None:
            n["type"] = type
        if mime is not None:
            n["mime"] = mime
        n["labels"].update(labels or {})
        return {"note_id": note_id}

    def _note_place(self, note_id: str, parents: list[str], remove: list[str] | None = None) -> dict:
        current = self.parents(note_id)
        drop = set(current) - set(parents) if remove is None else set(remove) - set(parents)
        self.notes[note_id]["parents"] = [p for p in current if p not in drop] + \
            [p for p in parents if p not in current]
        return {"note_id": note_id, "parents": self.notes[note_id]["parents"]}

    def _attr_set(self, note_id: str, name: str, value: str = "", inheritable: bool = False) -> dict:
        self.notes[note_id]["labels"][name] = value
        return {"note_id": note_id}

    def _attr_delete(self, note_id: str, name: str) -> dict:
        removed = self.notes[note_id]["labels"].pop(name, None) is not None
        return {"note_id": note_id, "name": name, "removed": int(removed)}

    def _note_delete(self, note_id: str) -> dict:
        del self.notes[note_id]
        return {"note_id": note_id, "deleted": True}


def make_board(tri: FakeTrilium, *, older: bool = True) -> dict[str, str]:
    """A Swarm board as `board_ensure` leaves it. Returns column title -> note id, plus board and Ledger.

    ``older`` adds the Older books, as "Older Done" and "Older Rejected".
    """
    awm = tri.add("AWM", type="book")
    board = tri.add("Swarm board", awm, type="book", labels={"swarmBoard": ""})
    out = {t: tri.add(t, board, type="book") for t in ("Proposed", "Planned", "Active", "Blocked",
                                                         "Pending review", "Done", "Rejected", "Ledger",
                                                         "By tag")}
    if older:
        for t in ("Done", "Rejected"):
            out[f"Older {t}"] = tri.add("Older (0)", out[t], type="book",
                                        labels={"swarmOlder": "", "cardStateAt": "0000"})
    return {**out, "board": board, "awm": awm}
