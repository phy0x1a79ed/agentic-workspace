"""One-off migrations of the board's existing cards, run by the host's admin.

`columns` files every card into the column folder for its old `#status`.
`threads` folds each chain of reply cards into the thread of the card at its
root. A reply card that carries `cardThreaded` was posted on the thread too,
so the plan leaves it out. Both are dry runs until told otherwise, and a second apply changes
nothing.

A thread migration cannot renumber a thread without moving the sequence numbers
clients hold as cursors. It does not shift them blindly: the messages a root
card already has keep their order and move up by the number of backfilled
ones, in one transaction (`Events.messages_backfill`), and the mirror notes are
rewritten to match. A client that saved a cursor on such a root sees its old
messages once more.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .cards import THREADED, Board
from .names import check_project
from .states import STATES, TERMINAL
from .vault import VaultError

log = logging.getLogger("awm.board.migrate")

_CARD_ID = re.compile(r"^[0-9a-f]{32}$")
_TESTS = re.compile(r"\b(tests?|testing|smoke|e2e|selftest|ping|probe|canary)\b", re.I)
_FACTORIO = re.compile(r"factorio", re.I)
_FEDERATION = re.compile(r"federat|\bswarms?\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def guess_project(title: str, body: str | None) -> str | None:
    """A project from a card's words. A title that names a test wins, since bodies mention tests in passing."""
    if _TESTS.search(title):
        return "tests"
    text = f"{title}\n{body or ''}"
    if _FACTORIO.search(text):
        return "factorio"
    if _FEDERATION.search(text):
        return "federation"
    return None


# -- columns ------------------------------------------------------------------


def columns(board: Board, dry_run: bool) -> dict:
    with board._guard:
        legacy = [rec for rec in board.vault.list() if rec["state"] is None]
        target = {rec["note_id"]: board._card({**rec, "content": None})["state"]
                  for rec in legacy}
        by_state = Counter(target.values())
        report = {"dry_run": dry_run,
                  "folders_to_create": [s for s in STATES if s not in board.vault.columns()],
                  "cards_to_move": len(legacy),
                  "by_state": {s: by_state[s] for s in STATES if by_state[s]}}
        if dry_run:
            return report
        board.vault.ensure_columns()
        for rec in legacy:
            if not rec["labels"].get("cardStateAt"):
                board.vault.update(rec["note_id"], labels={
                    "cardStateAt": rec["labels"].get("cardUpdated") or _now()})
            board.vault.place(rec, target[rec["note_id"]])
        return {**report, "moved": len(legacy)}


# -- threads ------------------------------------------------------------------


def _root_id(board: Board, card: dict, known: dict[str, dict] | None = None) -> str:
    """The id at the root of a card's `reply_to` chain. A missing parent or a cycle ends the walk."""
    known = known if known is not None else {}
    seen = {card["id"]}
    while card["reply_to"] and card["reply_to"] not in seen:
        parent = known.get(card["reply_to"])
        if parent is None:
            rec = board.vault.read(card["reply_to"])
            if rec is None:
                break
            parent = known[card["reply_to"]] = board._card(rec)
        card = parent
        seen.add(card["id"])
    return card["id"]


def _plan(board: Board) -> dict:
    """Every chain of reply cards, one subject per root, with the projects the words suggest."""
    known = {}
    for rec in board.vault.list(content=True):
        if rec["labels"].get(THREADED):
            continue                    # posted on this release: its thread holds it already
        card = board._card(rec)
        known[card["id"]] = card
    root_of: dict[str, str] = {}

    def root(card_id: str) -> str:
        if card_id not in root_of:
            root_of[card_id] = _root_id(board, known[card_id], known)
        return root_of[card_id]

    replies: dict[str, list[dict]] = {}
    subjects_at: set[str] = set()
    cyclic = 0
    for card_id in list(known):
        top = root(card_id)
        if top == card_id:
            subjects_at.add(card_id)
        elif root(top) == top:
            replies.setdefault(top, []).append(known[card_id])
        else:
            cyclic += 1
    subjects = [_subject(known[card_id], replies.get(card_id, []))
                for card_id in sorted(subjects_at | replies.keys(),
                                      key=lambda i: (known[i]["created_at"], i))]
    counts = Counter(s["project"] or "none" for s in subjects)
    summary = {"chains": len(subjects),
               "chains_with_replies": sum(1 for s in subjects if s["replies"]),
               "cards_folded": sum(len(s["replies"]) for s in subjects),
               "projects": dict(sorted(counts.items())),
               "to_done": sum(1 for s in subjects if s["to_done"]),
               "cyclic_skipped": cyclic}
    return {"version": 1, "created_at": _now(), "summary": summary, "subjects": subjects}


def _subject(card: dict, replies: list[dict]) -> dict:
    project = card["project"] or guess_project(card["title"], card["body"])
    replies = sorted(replies, key=lambda c: (c["created_at"], c["id"]))
    return {"root": card["id"], "title": card["title"], "state": card["state"],
            "project": project,
            "to_done": project == "tests" and card["state"] not in TERMINAL,
            "replies": [{"id": c["id"], "created_at": c["created_at"],
                         "from": c["sender"]["swarm"], "title": c["title"]} for c in replies]}


def _load(path: Path) -> dict:
    try:
        proposal = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read the proposal {path}: {e}") from e
    subjects = proposal.get("subjects") if isinstance(proposal, dict) else None
    if proposal.get("version") != 1 or not isinstance(subjects, list):
        raise ValueError(f"{path} is not a version 1 thread proposal")
    for subject in subjects:
        ids = [subject.get("root"), *(r.get("id") for r in subject.get("replies") or [])]
        if not all(isinstance(i, str) and _CARD_ID.match(i) for i in ids):
            raise ValueError(f"the proposal holds a bad card id under {subject.get('root')!r}")
        check_project(subject.get("project"))
    return proposal


def threads(board: Board, dry_run: bool, proposal_path: str | Path | None) -> dict:
    if dry_run:
        with board._guard:
            proposal = _plan(board)
        if proposal_path:
            path = Path(proposal_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(proposal, indent=1))
        return {"dry_run": True, "proposal_path": str(proposal_path) if proposal_path else None,
                **proposal["summary"]}
    if not proposal_path:
        raise ValueError("proposal_path is required to apply a proposal")
    proposal = _load(Path(proposal_path))
    report: Counter = Counter()
    for subject in proposal["subjects"]:
        _apply(board, subject, report)
    return {"dry_run": False, "proposal_path": str(proposal_path), **report}


def _apply(board: Board, subject: dict, report: Counter) -> None:
    with board._guard:
        root_rec = board.vault.read(subject["root"])
        if root_rec is None:
            report["missing_roots"] += 1
            return
        replies = []
        for item in subject.get("replies") or []:
            rec = board.vault.read(item["id"])
            card = board._card(rec) if rec else None
            if card is None or _root_id(board, card) != subject["root"]:
                report["replies_left_alone"] += 1
                continue
            replies.append((rec, card))
        replies.sort(key=lambda rc: (rc[1]["created_at"], rc[1]["id"]))
        _fold(board, root_rec, replies, report)
        for rec, _ in replies:
            if "archived" not in rec["labels"]:
                board.vault.update(rec["note_id"], labels={
                    "archived": "", "cardFolded": subject["root"]})
                report["replies_archived"] += 1
        root_rec = board.vault.read(subject["root"])
        root = board._card(root_rec)
        project = subject.get("project")
        if project and root["project"] != project:
            board.vault.update(root_rec["note_id"], labels={"project": project})
            report["projects_set"] += 1
        if subject.get("to_done") and root["state"] not in TERMINAL:
            board._transition(root_rec, root, "done", "card.moved")
            report["moved_to_done"] += 1


def _fold(board: Board, root_rec: dict, replies: list[tuple[dict, dict]], report: Counter) -> None:
    """Backfill the replies as the head of the root's thread, unless they are already there."""
    done = board.events.folded_ids([card["id"] for _, card in replies])
    pending = [card for _, card in replies if card["id"] not in done]
    if not pending:
        return
    if done:
        report["stale_subjects"] += 1
        log.warning("card %s: some replies were folded earlier and some were not; "
                    "run a new dry run", root_rec["labels"]["cardId"])
        return
    history = [{"reply_id": card["id"], "at": card["created_at"], "sender": card["sender"],
                "body": (card["body"] or "").strip() and card["body"] or card["title"]}
               for card in pending]
    thread = board.events.messages_backfill(root_rec["labels"]["cardId"], history)
    report["messages_backfilled"] += len(history)
    try:
        for message in thread:
            board._mirror(root_rec["note_id"], message)
        board.vault.update(root_rec["note_id"], labels={"cardLastMsg": str(thread[-1]["seq"])})
    except VaultError as e:
        log.warning("thread of card %s is recorded but not fully mirrored: %s",
                    root_rec["labels"]["cardId"], e)
