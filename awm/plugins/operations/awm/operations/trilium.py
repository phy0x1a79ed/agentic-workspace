"""The board's way into deneb's vault: the ``trilium`` tool, never ETAPI (fleet.md R105, R87 item 3).

alcor holds no vault token. Each call goes through alcor's gateway to the ``trilium``
tool on deneb, a singleton service that gives alcor no edge.
"""

from __future__ import annotations

from typing import Any

from awm.operations.gateway import GatewayTool
from awm.operations.relay import Refused, Unreachable


def _missing(exc: Exception) -> bool:
    """Whether a failed call says the note does not exist.

    deneb's door returns ETAPI's 404 as a 502 whose body names ``NOTE_NOT_FOUND``,
    which the gateway client raises as Unreachable, not Refused.
    """
    text = str(exc)
    if isinstance(exc, Unreachable):
        return "NOTE_NOT_FOUND" in text
    return "404" in text or "not found" in text.lower()


class TriliumTool:
    """The few note verbs the writer, ``board-install`` and the spawn body check use."""

    def __init__(self, peer: str | None = "deneb", tool: Any = None) -> None:
        self.tool = tool or GatewayTool("trilium", peer=peer, timeout=20)

    def _call(self, verb: str, **args: Any) -> Any:
        return self.tool.call_sync(verb, {k: v for k, v in args.items() if v is not None})

    def note(self, note_id: str) -> dict[str, Any] | None:
        """The note with its attributes and no body, or None when the vault has none by that id."""
        try:
            return self._call("note_get", note_id=note_id, content=False)
        except (Refused, Unreachable) as exc:
            if _missing(exc):
                return None
            raise

    def content(self, note_id: str) -> str:
        return str(self._call("note_get", note_id=note_id).get("content") or "")

    def create_note(self, parent: str, title: str, type_: str = "code", content: str = "", *,
                    note_id: str | None = None, mime: str | None = None,
                    labels: dict[str, str] | None = None) -> str:
        """Create a note and return its id. A fixed ``note_id`` lets a re-run find it again."""
        return self._call("note_create", parent=parent, note_id=note_id, title=title, type=type_,
                          content=content, mime=mime, labels=labels or None)["note_id"]

    def update(self, note_id: str, *, title: str | None = None, content: str | None = None,
               mime: str | None = None, labels: dict[str, str] | None = None) -> None:
        self._call("note_update", note_id=note_id, title=title, content=content, mime=mime,
                   labels=labels or None)

    def move(self, note_id: str, parent: str) -> None:
        """Put the note under this parent and nowhere else."""
        self._call("note_move", note_id=note_id, parent=parent)

    def label_delete(self, note_id: str, name: str) -> None:
        self._call("attr_delete", note_id=note_id, name=name)

    def relation(self, note_id: str, name: str, target: str) -> None:
        self._call("attr_set", note_id=note_id, name=name, value=target, type="relation")

    def children(self, note_id: str) -> list[dict[str, Any]]:
        return list((self._call("note_children", note_id=note_id) or {}).get("children") or [])

    def delete_note(self, note_id: str) -> None:
        try:
            self._call("note_delete", note_id=note_id)
        except (Refused, Unreachable) as exc:
            if not _missing(exc):
                raise
