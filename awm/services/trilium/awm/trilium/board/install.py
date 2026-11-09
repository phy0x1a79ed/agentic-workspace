"""The pages of the board and of Ops, as notes in the vault (queue.md R61, fleet.md R147).

``install`` is idempotent and writes only what differs, so the writer runs it at each start and
``awm trilium board_install`` runs it by hand. The pages are code notes the vault executes: a
render note names its page in a ``renderNote`` relation, the page holds a frontend script, and one
script note with ``#run=frontendStartup`` adds the bot button to every ordinary note.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from typing import Any

from awm.trilium.board import ids
from awm.trilium.board.fence import Fence

DEFAULT_ALCOR_ORIGIN = "https://auth.tony-xy-liu.com"
ASSETS = resources.files("awm.trilium.board") / "assets"


def alcor_origin() -> str:
    return (os.environ.get("AWM_TRILIUM_ALCOR_ORIGIN") or DEFAULT_ALCOR_ORIGIN).rstrip("/")


def _asset(name: str) -> str:
    """A page, with the shared CSS in its stylesheet or the shared library in front of its script.

    The overlays, toasts and menus are appended to the document, outside the render note's own
    container, so the script also puts the shared CSS in the document head."""
    text = (ASSETS / name).read_text()
    if name.endswith(".html"):
        return text.replace("/*COMMON*/", (ASSETS / "common.css").read_text())
    css = json.dumps((ASSETS / "common.css").read_text())
    return f"const AWM_COMMON_CSS = {css};\n" + (ASSETS / "lib.js").read_text() + "\n" + text


def _code(fence: Fence, parent: str, note_id: str, title: str, mime: str, text: str,
          labels: dict[str, str | None] | None = None) -> None:
    if fence.note(note_id) is None:
        fence.create(parent, title, "code", text, note_id=note_id, mime=mime, labels=labels)
    else:
        fence.update(note_id, title=title, mime=mime, content=text, labels=labels)


def _render_note(fence: Fence, parent: str, note_id: str, title: str,
                 labels: dict[str, str | None]) -> None:
    if fence.note(note_id) is None:
        fence.create(parent, title, "render", note_id=note_id, labels=labels)
    else:
        fence.update(note_id, title=title, type_="render", labels=labels)


def ensure_tasks(fence: Fence, parent: str = ids.BOOK_ID) -> None:
    """The tasks book and one folder per column. They are read-only to Tony in the vault."""
    if fence.note(ids.TASKS_ID) is None:
        fence.create(parent, "tasks", "book", note_id=ids.TASKS_ID,
                     labels={**ids.MANAGED, "swarmTasks": ""})
    elif parent in fence.roots:
        fence.nest_root(ids.TASKS_ID, parent)
    for section, note_id, title in ids.COLUMNS:
        if fence.note(note_id) is None:
            fence.create(ids.TASKS_ID, title, "book", note_id=note_id,
                         labels={**ids.MANAGED, "swarmColumn": section})


def _fold_stale_column(fence: Fence) -> None:
    if fence.note(ids.LEGACY_STALE_ID) is None:
        return
    history = ids.COLUMN_IDS["history"]
    for kid in fence.children(ids.LEGACY_STALE_ID):
        labels = {a["name"]: a.get("value") for a in kid.get("attributes") or [] if a.get("type") == "label"}
        fence.update(kid["noteId"], labels={"section": "history", "status": "stale", "historyAt": labels.get("updatedAt")})
        fence.move(kid["noteId"], history)
    fence.delete(ids.LEGACY_STALE_ID)


def install(fence: Fence, parent: str = ids.BOOK_ID) -> dict[str, Any]:
    """Create or update the board page, the Ops page and the bot button script."""
    origin = alcor_origin()
    labels = {"alcorOrigin": origin, "approverOrigin": None}
    _render_note(fence, parent, ids.ROOT_ID, "Swarm board", {"swarmBoard": "", **labels})
    _code(fence, ids.ROOT_ID, ids.PAGE_ID, "board page", "text/html", _asset("board.html"))
    _code(fence, ids.PAGE_ID, ids.SCRIPT_ID, "board.js", ids.FRONTEND_JS, _asset("board.js"))
    fence.relation(ids.ROOT_ID, "renderNote", ids.PAGE_ID)
    _code(fence, ids.ROOT_ID, ids.BOT_SCRIPT_ID, "bot button.js", ids.FRONTEND_JS,
          _asset("bot.js"), labels={"run": "frontendStartup", "swarmBot": ""})

    _render_note(fence, parent, ids.OPS_ID, "Ops", {"swarmOps": "", **labels})
    _code(fence, ids.OPS_ID, ids.OPS_PAGE_ID, "ops page", "text/html", _asset("ops.html"),
          labels=ids.MANAGED)
    _code(fence, ids.OPS_PAGE_ID, ids.OPS_SCRIPT_ID, "ops.js", ids.FRONTEND_JS, _asset("ops.js"))
    fence.relation(ids.OPS_ID, "renderNote", ids.OPS_PAGE_ID)
    ensure_tasks(fence, ids.ROOT_ID)
    _fold_stale_column(fence)
    return {"board": ids.ROOT_ID, "ops": ids.OPS_ID, "tasks": ids.TASKS_ID,
            "bot": ids.BOT_SCRIPT_ID, "alcor_origin": origin}
