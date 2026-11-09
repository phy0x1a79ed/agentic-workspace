"""Fixed note ids, so a re-run of the writer finds the note it made (queue.md R61)."""

from __future__ import annotations

BOOK_ID = "iF0A3SWEGqUx"
ROOT_ID = "awmSwarmBoard"
TASKS_ID = "awmSwarmTasks"
PAGE_ID = "awmSwarmBoardPage"
SCRIPT_ID = "awmSwarmBoardJs"
BOT_SCRIPT_ID = "awmSwarmBotJs"
OPS_ID = "awmSwarmOps"
OPS_PAGE_ID = "awmSwarmOpsPage"
OPS_SCRIPT_ID = "awmSwarmOpsJs"
ROSTER_ID = "awmSwarmRoster"
PORTALS_ID = "awmSwarmPortals"

#: The three subtrees the writer owns. Nothing else is written, bar the two acts of fleet.md R106.
ROOTS = (TASKS_ID, OPS_ID, ROOT_ID)

#: Column folders under tasks, in board order: (queue section, folder note id, folder title).
COLUMNS = (("needs_you", "awmSwarmColNeedsYou", "Needs you"),
           ("working", "awmSwarmColWorking", "Working"),
           ("history", "awmSwarmColHistory", "History"))
COLUMN_IDS = {section: note_id for section, note_id, _ in COLUMNS}

#: The folder stale cards sat in before History took them.
LEGACY_STALE_ID = "awmSwarmColStale"

MANAGED = {"agentManaged": "", "readOnly": ""}
MARKDOWN = "text/x-markdown"
FRONTEND_JS = "application/javascript;env=frontend"


def card_note_id(task_id: str) -> str:
    """A note id that ETAPI accepts for any task id."""
    return "swc" + task_id.encode().hex()


def record_note_id(task_id: str) -> str:
    return "swr" + task_id.encode().hex()


def messages_note_id(task_id: str) -> str:
    return "swm" + task_id.encode().hex()


def marker_note_id(task_id: str) -> str:
    return "swk" + task_id.encode().hex()
