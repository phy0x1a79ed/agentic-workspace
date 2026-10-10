"""Stand-ins for the board, cx and the personas file."""

from __future__ import annotations

from types import SimpleNamespace

from awm.board.client import BoardConflict, BoardRefused
from awm.representative.sessions import CxUnavailable

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
        self.rows.append({"job": job, "name": spec["name"], "mode": spec["mode"], "state": "idle"})
        return {"ok": True, "job": job, "mode": spec["mode"]}


def session_row(mode: str, job: str = "abc12345", state: str = "idle", name: str | None = None) -> dict:
    return {"job": job, "name": name or mode, "mode": mode, "state": state}
