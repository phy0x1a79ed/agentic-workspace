"""Lineage verbs for the gateway manifest (sessions.md R13, R16)."""

from __future__ import annotations

from pathlib import Path

from awm.journal import channel, lineage
from awm.journal.operations.posts import _caller_session

_HIDDEN = ["cli", "http"]

LINEAGE_MANIFEST_FUNCTIONS = [
    {
        "name": "lineage",
        "tool": "journal_lineage",
        "effect": "read",
        "description": (
            "A session's lineage, walked forward only: the session that started it (or "
            "that it was before a clear), that session's parent and so on, and the "
            "journals its debrief cites, then what those sessions came from. "
            "session defaults to yours."
        ),
        "params": [{"name": "session", "type": "string", "required": False}],
    },
    {
        "name": "session_parent",
        "tool": "journal_session_parent",
        "effect": "write",
        "surfaces": _HIDDEN,
        "description": ("Record who started a session, or which session it was before a "
                        "clear. For reflection and the harness, not agents. The first link wins."),
        "params": [
            {"name": "session_id", "type": "string", "required": True},
            {"name": "parent", "type": "string", "required": True},
            {"name": "how", "type": "string", "required": False,
             "description": "start or clear. Default start."},
            {"name": "node", "type": "string", "required": False},
        ],
    },
    {
        "name": "lineage_import",
        "tool": "journal_lineage_import",
        "effect": "write",
        "surfaces": _HIDDEN,
        "description": "Copy the parent links reporting kept (its session_parents table) once.",
        "params": [{"name": "path", "type": "string", "required": True,
                    "description": "The reporting work database to read."}],
    },
]


def _local(as_: str | None) -> None:
    if channel.edge_origin(as_):
        raise PermissionError("lineage stays on its own node")


def _handle_lineage(args: dict, as_: str | None = None) -> dict:
    _local(as_)
    session = args.get("session") or _caller_session(args.get("_caller_pid"))
    if not session:
        raise ValueError("no calling session; pass session")
    return lineage.walk(session)


def _handle_session_parent(args: dict, as_: str | None = None) -> dict:
    _local(as_)
    made = lineage.set_parent(args["session_id"], args["parent"], args.get("how") or "start",
                              args.get("node") or "")
    return {"ok": True, "recorded": made}


def _handle_lineage_import(args: dict, as_: str | None = None) -> dict:
    _local(as_)
    return lineage.import_parents(Path(args["path"]))


LINEAGE_HANDLERS = {
    "lineage": _handle_lineage,
    "session_parent": _handle_session_parent,
    "lineage_import": _handle_lineage_import,
}
