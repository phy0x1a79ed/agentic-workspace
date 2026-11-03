"""The Swarm board in Trilium: its note ids and ``board-install`` (queue.md R61, R62).

The board root is a render note labelled ``#swarmBoard``. The card notes are its children.
The page itself is an HTML code note with a frontend script child, under the root.
"""

from __future__ import annotations

from importlib import resources

from awm.taskqueue.etapi import Etapi

ROOT_ID = "awmSwarmBoard"
PAGE_ID = "awmSwarmBoardPage"
SCRIPT_ID = "awmSwarmBoardJs"
FILES = resources.files("awm.taskqueue") / "board"


def card_note_id(task_id: str) -> str:
    """A note id that ETAPI accepts for any task id, so a re-run finds the same note."""
    return "swc" + task_id.encode().hex()


def record_note_id(task_id: str) -> str:
    return "swr" + task_id.encode().hex()


def ensure_root(api: Etapi, parent: str = "root") -> dict:
    note = api.note(ROOT_ID)
    if note is None:
        api.create_note(parent, ROOT_ID, "Swarm", "render")
        note = api.note(ROOT_ID)
    api.sync_attributes(note, {("label", "swarmBoard"): ""}, {("label", "swarmBoard")})
    return api.note(ROOT_ID)


def _upsert_code(api: Etapi, parent: str, note_id: str, title: str, mime: str, text: str) -> None:
    if api.note(note_id) is None:
        api.create_note(parent, note_id, title, "code", text, mime=mime)
    else:
        api.patch_note(note_id, title=title, mime=mime)
        api.set_content(note_id, text)


def install(api: Etapi, approver_origin: str, parent: str = "root") -> dict:
    """Create or update the board page. Run it again after each change to ``board/``."""
    root = ensure_root(api, parent)
    _upsert_code(api, ROOT_ID, PAGE_ID, "board page", "text/html",
                 (FILES / "board.html").read_text())
    _upsert_code(api, PAGE_ID, SCRIPT_ID, "board.js", "application/javascript;env=frontend",
                 (FILES / "board.js").read_text())
    managed = {("label", "approverOrigin"), ("relation", "renderNote")}
    api.sync_attributes(root, {("label", "approverOrigin"): approver_origin.rstrip("/"),
                               ("relation", "renderNote"): PAGE_ID}, managed)
    return {"root": ROOT_ID, "page": PAGE_ID, "script": SCRIPT_ID}
