"""The Swarm board in Trilium: its note ids and ``board-install`` (queue.md R61, R62, R67).

The board sits in Tony's AWM book (``BOOK_ID``), in three notes side by side:

- ``tasks``, a book with one folder per column. Each card is a note in its column's folder.
- ``Ops``, the swarm status book. It holds the services' notices and one note of static
  links to each box's portal, and no password (fleet.md R81).
- ``Swarm board``, the render note labelled ``#swarmBoard``. Its ``renderNote`` relation
  names the page, an HTML code note with a frontend script child.

deneb lets alcor's writer reach only those three subtrees (``AWM_TRILIUM_WRITER_ROOTS``).
"""

from __future__ import annotations

from importlib import resources

from awm.operations.trilium import TriliumTool

BOOK_ID = "iF0A3SWEGqUx"
ROOT_ID = "awmSwarmBoard"
TASKS_ID = "awmSwarmTasks"
PAGE_ID = "awmSwarmBoardPage"
SCRIPT_ID = "awmSwarmBoardJs"
OPS_ID = "awmSwarmOps"
PORTALS_ID = "awmSwarmPortals"
MANAGED = {"agentManaged": "", "readOnly": ""}
#: Column folders under tasks, in board order: (queue section, folder note id, folder title).
COLUMNS = (("proposed", "awmSwarmColProposed", "Proposed"),
           ("needs_you", "awmSwarmColNeedsYou", "Needs you"),
           ("working", "awmSwarmColWorking", "Working"),
           ("done", "awmSwarmColDone", "Done"),
           ("triage", "awmSwarmColTriage", "Swarm triage"))
COLUMN_IDS = {section: note_id for section, note_id, _ in COLUMNS}
FILES = resources.files("awm.operations") / "board"


def card_note_id(task_id: str) -> str:
    """A note id that ETAPI accepts for any task id, so a re-run finds the same note."""
    return "swc" + task_id.encode().hex()


def record_note_id(task_id: str) -> str:
    return "swr" + task_id.encode().hex()


def messages_note_id(task_id: str) -> str:
    return "swm" + task_id.encode().hex()


def ensure_root(api: TriliumTool, parent: str = BOOK_ID) -> None:
    """The board's render note, under ``parent`` when it has to be made."""
    if api.note(ROOT_ID) is None:
        api.create_note(parent, "Swarm board", "render", note_id=ROOT_ID,
                        labels={"swarmBoard": ""})
    else:
        api.update(ROOT_ID, labels={"swarmBoard": ""})


def ensure_tasks(api: TriliumTool, parent: str = BOOK_ID) -> None:
    """The tasks book and one folder per column. They are read-only to Tony in the vault."""
    if api.note(TASKS_ID) is None:
        api.create_note(parent, "tasks", "book", note_id=TASKS_ID,
                        labels={**MANAGED, "swarmTasks": ""})
    for section, note_id, title in COLUMNS:
        if api.note(note_id) is None:
            api.create_note(TASKS_ID, title, "book", note_id=note_id,
                            labels={**MANAGED, "swarmColumn": section})


def portals_text(portals: dict[str, str]) -> str:
    """The Portals note: one link per box, from the approver's ``portals`` {host: node}."""
    rows = [f"- [{node}](https://{host}/)" for host, node in sorted(portals.items(),
                                                                   key=lambda kv: kv[1])]
    return "# Portals\n\nEach box's portal. Sign in there with your passkey.\n\n" + (
        "\n".join(rows) or "No portal is configured.") + "\n"


def ensure_ops(api: TriliumTool, portals: dict[str, str], parent: str = BOOK_ID) -> None:
    """The Ops book beside tasks, with its Portals note up to date."""
    if api.note(OPS_ID) is None:
        api.create_note(parent, "Ops", "book", note_id=OPS_ID, labels={"swarmOps": ""})
    _upsert_code(api, OPS_ID, PORTALS_ID, "Portals", "text/x-markdown", portals_text(portals))


def _upsert_code(api: TriliumTool, parent: str, note_id: str, title: str, mime: str,
                 text: str) -> None:
    if api.note(note_id) is None:
        api.create_note(parent, title, "code", text, note_id=note_id, mime=mime)
    else:
        api.update(note_id, title=title, mime=mime, content=text)


def install(api: TriliumTool, approver_origin: str, parent: str = BOOK_ID) -> dict:
    """Create or update the board page through the trilium tool. Run it again after each change to ``board/``."""
    ensure_root(api, parent)
    _upsert_code(api, ROOT_ID, PAGE_ID, "board page", "text/html",
                 (FILES / "board.html").read_text())
    _upsert_code(api, PAGE_ID, SCRIPT_ID, "board.js", "application/javascript;env=frontend",
                 (FILES / "board.js").read_text())
    api.update(ROOT_ID, title="Swarm board", labels={"approverOrigin": approver_origin.rstrip("/")})
    api.relation(ROOT_ID, "renderNote", PAGE_ID)
    return {"root": ROOT_ID, "page": PAGE_ID, "script": SCRIPT_ID}
