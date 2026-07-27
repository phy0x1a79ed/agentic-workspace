"""Declarative editing operations — what ``drawio edit`` actually applies.

The agent-facing shape of an edit is a list of plain dicts::

    [{"op": "add_node", "page": "Carbon", "id": "mol/glucose",
      "label": "glucose", "right_of": "mol/g6p", "gap": 60},
     {"op": "add_edge", "page": "Carbon", "id": "mol/e1",
      "from": "mol/glucose", "to": "mol/g6p"}]

which keeps the whole workflow inside awm tooling — no filesystem path, no XML,
no git. The agent can still ask for the path when it wants to look at the file
and the URL when it wants to look at the render; it just never *has* to.

Operations are idempotent by cell id, so replaying a build is a no-op rather
than a duplication, and they never touch cells they were not given. Both
properties come from :mod:`awm.drawio.dwg` and are what make an agent's repeated
iteration safe inside a checkout.

The whole list applies as a unit: if any operation fails, nothing is written.
A half-applied build is worse than a rejected one, because the agent has no way
to tell which half landed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import dwg


class OpError(ValueError):
    """An operation was malformed or referred to something that isn't there."""


def _require(op: dict, key: str, index: int) -> Any:
    if key not in op or op[key] in (None, ""):
        raise OpError(f"op[{index}] {op.get('op')!r}: {key!r} is required")
    return op[key]


def _page(doc: dwg.Doc, op: dict, index: int) -> dwg.Page:
    name = op.get("page")
    pages = doc.pages()
    if not pages:
        raise OpError("diagram has no pages")
    if name is None:
        return pages[0]
    try:
        return doc.page(name)
    except KeyError as exc:
        raise OpError(f"op[{index}]: {exc}") from exc


def _pair(op: dict, key: str) -> tuple[float, float] | None:
    value = op.get(key)
    if value is None:
        return None
    try:
        a, b = value
        return float(a), float(b)
    except (TypeError, ValueError) as exc:
        raise OpError(f"{key!r} must be a two-number list like [120, 40]") from exc


# --- individual operations -------------------------------------------------

def _op_add_node(doc: dwg.Doc, op: dict, index: int) -> str:
    page = _page(doc, op, index)
    cell_id = _require(op, "id", index)
    page.add_node(
        cell_id,
        label=op.get("label", ""),
        at=_pair(op, "at"),
        size=_pair(op, "size") or dwg.DEFAULT_NODE_SIZE,
        style=op.get("style", dwg.DEFAULT_NODE_STYLE),
        parent=op.get("parent", "1"),
        right_of=op.get("right_of"),
        below=op.get("below"),
        in_=op.get("in"),
        gap=float(op.get("gap", dwg.DEFAULT_GAP)),
    )
    return f"add_node {cell_id} on {page.name!r}"


def _op_add_edge(doc: dwg.Doc, op: dict, index: int) -> str:
    page = _page(doc, op, index)
    cell_id = _require(op, "id", index)
    src, dst = _require(op, "from", index), _require(op, "to", index)
    page.add_edge(
        cell_id, src, dst,
        style=op.get("style", dwg.DEFAULT_EDGE_STYLE),
        label=op.get("label", ""),
        parent=op.get("parent", "1"),
    )
    return f"add_edge {cell_id}: {src} -> {dst}"


def _op_set(doc: dwg.Doc, op: dict, index: int) -> str:
    cell_id = _require(op, "id", index)
    try:
        doc.set(
            cell_id,
            page=op.get("page"),
            label=op.get("label"),
            style=op.get("style"),
            move=_pair(op, "move"),
            size=_pair(op, "size"),
        )
    except KeyError as exc:
        raise OpError(f"op[{index}] set: {exc}") from exc
    return f"set {cell_id}"


def _op_remove(doc: dwg.Doc, op: dict, index: int) -> str:
    cell_id = _require(op, "id", index)
    if not doc.remove(cell_id, op.get("page")):
        raise OpError(f"op[{index}] remove: no cell {cell_id!r}")
    return f"remove {cell_id}"


def _op_remove_prefix(doc: dwg.Doc, op: dict, index: int) -> str:
    prefix = _require(op, "prefix", index)
    name = op.get("page")
    count = (doc.page(name).remove_prefix(prefix) if name
             else sum(p.remove_prefix(prefix) for p in doc.pages()))
    return f"remove_prefix {prefix!r}: {count} cell(s)"


def _op_add_page(doc: dwg.Doc, op: dict, index: int) -> str:
    name = _require(op, "name", index)
    doc.add_page(name, int(op.get("width", 6000)), int(op.get("height", 900)))
    return f"add_page {name!r}"


def _op_layout(doc: dwg.Doc, op: dict, index: int) -> str:
    page = _page(doc, op, index)
    moved = page.layout(prog=op.get("algo", "dot"),
                        rankdir=op.get("rankdir", "LR"))
    return f"layout {page.name!r}: moved {moved} cell(s)"


OPS: dict[str, Callable[[dwg.Doc, dict, int], str]] = {
    "add_node": _op_add_node,
    "add_edge": _op_add_edge,
    "set": _op_set,
    "remove": _op_remove,
    "remove_prefix": _op_remove_prefix,
    "add_page": _op_add_page,
    "layout": _op_layout,
}


def apply(path: Path, operations: list[dict]) -> dict:
    """Apply ``operations`` to the diagram at ``path``, all-or-nothing.

    The document is mutated in memory and only written once every operation has
    succeeded, so a failure partway through leaves the file exactly as it was.
    """
    if not isinstance(operations, list) or not operations:
        raise OpError("edit needs a non-empty list of operations")

    doc = dwg.Doc.open(path)
    applied = []
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise OpError(f"op[{index}] is not an object")
        name = op.get("op")
        handler = OPS.get(name or "")
        if handler is None:
            raise OpError(
                f"op[{index}]: unknown operation {name!r} "
                f"(known: {', '.join(sorted(OPS))})"
            )
        applied.append(handler(doc, op, index))

    warnings = doc.save()
    return {"applied": applied, "warnings": warnings, "count": len(applied)}
