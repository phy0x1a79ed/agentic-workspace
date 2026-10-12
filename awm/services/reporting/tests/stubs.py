"""Stand-ins for the board, cx and the personas file."""

from __future__ import annotations

from types import SimpleNamespace

from awm.board.client import BoardConflict, BoardRefused
from awm.reporting.sessions import CxUnavailable

_N = 0


def make_card(**over) -> dict:
    global _N
    _N += 1
    card = {
        "id": f"{_N:032x}", "kind": "request", "recipient": "tony", "status": "posted",
        "priority": "normal", "title": f"card {_N}", "body": "do the thing",
        "sender": {"swarm": "beta", "principal": "op", "party": "p"},
        "claimant": None, "created_at": "2026-10-09T12:00:00.000+00:00",
    }
    card.update(over)
    return card


class FakeBoard:
    """Just enough of BoardClient: claims, reads, and a scripted stream."""

    def __init__(self, swarm: str = "tony") -> None:
        self.swarm = swarm
        self.cards: dict[str, dict] = {}
        self.events: list[tuple[int, str, dict]] = []
        self.conflicts: set[str] = set()
        self.claim_error: Exception | None = None
        self.claims: list[str] = []
        self.stream_starts: list[int | None] = []
        self.list_calls: list[dict] = []
        self.list_error: Exception | None = None
        self.get_errors: dict[str, Exception] = {}

    def add(self, card: dict) -> dict:
        self.cards[card["id"]] = card
        return card

    def post_event(self, type_: str, card: dict, event_id: int | None = None) -> int:
        event_id = event_id or (self.events[-1][0] + 1 if self.events else 1)
        self.events.append((event_id, type_, dict(card)))
        return event_id

    async def claim(self, card_id: str) -> dict:
        self.claims.append(card_id)
        if self.claim_error is not None:
            raise self.claim_error
        if card_id in self.conflicts:
            raise BoardConflict(409, "the card is already held")
        card = self.cards.get(card_id)
        if card is None:
            raise BoardRefused(404, "not found")
        card["status"], card["claimant"] = "in_progress", self.swarm
        return dict(card)

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
                   if v and k in ("recipient", "kind", "status", "claimant")):
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


REPRESENTATIVE = {
    "name": "front-door", "mode": "representative", "model": "sonnet[1m]",
    "permission": "dontAsk", "disallowed_tools": ["Bash", "Edit", "Write"],
    "remote_control": True, "prompt": "triage", "project": "awm", "scope": "door",
}
SECRETARY = {
    "name": "secretary", "mode": "secretary", "model": "sonnet[1m]",
    "permission": "dontAsk", "disallowed_tools": ["Bash", "Edit", "Write"],
    "remote_control": True, "prompt": "assist", "project": "awm", "scope": "door",
}


def personas(**over) -> SimpleNamespace:
    return SimpleNamespace(**{"REPRESENTATIVE": dict(REPRESENTATIVE),
                              "SECRETARY": dict(SECRETARY), **over})


class FakeCx:
    """cx as the door sees it: `list` and `start`, with sessions that stay started.

    Rows given up front are sessions somebody else started, unless a test records
    them in the door's queue.
    """

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.started: list[dict] = []
        self.unavailable = False
        self.refuse: str | None = None
        self.refuse_stop: str | None = None
        self.stopped: list[str] = []
        self._n = 0

    async def list(self) -> list[dict]:
        if self.unavailable:
            raise CxUnavailable("cx is down")
        return [dict(r) for r in self.rows]

    async def start(self, spec: dict) -> dict:
        self.started.append(dict(spec))
        if self.refuse:
            return {"ok": False, "reason": self.refuse}
        self._n += 1
        job = f"job{self._n:04d}"
        self.rows.append({"job": job, "name": spec["name"], "mode": spec["mode"], "state": "idle",
                          "caller": "local", "parent": None})
        return {"ok": True, "job": job, "mode": spec["mode"]}


    async def stop(self, job: str) -> dict:
        self.stopped.append(job)
        if self.refuse_stop:
            return {"ok": False, "reason": self.refuse_stop}
        for row in self.rows:
            if row.get("job") == job:
                row["state"] = "gone"
        return {"ok": True, "job": job}


def session_row(mode: str, job: str = "abc12345", state: str = "idle", name: str | None = None,
                caller: str | None = None, parent: str | None = None,
                needs: str | None = None, detail: str | None = None) -> dict:
    """A cx list row. A hand-started session has no lineage, so no caller."""
    return {"job": job, "name": name or mode, "mode": mode, "state": state,
            "caller": caller, "parent": parent, "needs": needs, "detail": detail}


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
               "attributes": [{"name": k, "value": v, "type": "label"} for k, v in n["labels"].items()]}
        if content:
            out["content"] = n.get("content", "")
        return out

    def _note_create(self, title: str, parent: str = "root", type: str = "text", content: str = "",
                     mime: str | None = None, labels: dict | None = None, relations: dict | None = None) -> dict:
        return {"note_id": self.add(title, parent, type=type, content=content, labels={**(labels or {})},
                                    mime=mime), "created": True}

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

    def _note_delete(self, note_id: str) -> dict:
        del self.notes[note_id]
        return {"note_id": note_id, "deleted": True}


def make_board(tri: FakeTrilium) -> dict[str, str]:
    """A Swarm board as `board_ensure` leaves it. Returns column title -> note id, plus board and Ledger."""
    awm = tri.add("AWM", type="book")
    board = tri.add("Swarm board", awm, type="book", labels={"swarmBoard": ""})
    out = {t: tri.add(t, board, type="book") for t in ("Proposed", "Planned", "Active", "Blocked",
                                                         "Pending review", "Ledger")}
    return {**out, "board": board, "awm": awm}
