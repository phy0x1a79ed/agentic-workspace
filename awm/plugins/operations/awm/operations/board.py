"""The Swarm board in Trilium: its note ids and ``board-install`` (queue.md R61, R62).

The board root is a render note labelled ``#swarmBoard``. The card notes are its children.
The page itself is an HTML code note with a frontend script child, under the root.
"""

from __future__ import annotations

from importlib import resources

from awm.operations.trilium import TriliumTool

ROOT_ID = "awmSwarmBoard"
PAGE_ID = "awmSwarmBoardPage"
SCRIPT_ID = "awmSwarmBoardJs"
FILES = resources.files("awm.operations") / "board"


def card_note_id(task_id: str) -> str:
    """A note id that ETAPI accepts for any task id, so a re-run finds the same note."""
    return "swc" + task_id.encode().hex()


def record_note_id(task_id: str) -> str:
    return "swr" + task_id.encode().hex()


def messages_note_id(task_id: str) -> str:
    return "swm" + task_id.encode().hex()


def ensure_root(api: TriliumTool, parent: str = "root") -> None:
    if api.note(ROOT_ID) is None:
        api.create_note(parent, "Swarm", "render", note_id=ROOT_ID, labels={"swarmBoard": ""})
    else:
        api.update(ROOT_ID, labels={"swarmBoard": ""})


def _upsert_code(api: TriliumTool, parent: str, note_id: str, title: str, mime: str,
                 text: str) -> None:
    if api.note(note_id) is None:
        api.create_note(parent, title, "code", text, note_id=note_id, mime=mime)
    else:
        api.update(note_id, title=title, mime=mime, content=text)


def install(api: TriliumTool, approver_origin: str, parent: str = "root") -> dict:
    """Create or update the board page through the trilium tool. Run it again after each change to ``board/``."""
    ensure_root(api, parent)
    _upsert_code(api, ROOT_ID, PAGE_ID, "board page", "text/html",
                 (FILES / "board.html").read_text())
    _upsert_code(api, PAGE_ID, SCRIPT_ID, "board.js", "application/javascript;env=frontend",
                 (FILES / "board.js").read_text())
    api.update(ROOT_ID, labels={"approverOrigin": approver_origin.rstrip("/")})
    api.relation(ROOT_ID, "renderNote", PAGE_ID)
    return {"root": ROOT_ID, "page": PAGE_ID, "script": SCRIPT_ID}
