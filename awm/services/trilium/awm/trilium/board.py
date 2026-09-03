"""Kanban boards in the vault, as Trilium already understands them.

Trilium ships a board view, so a board here is not a thing this service draws.
It is a `book` note carrying `#viewType=board`, and a card is any note beneath
it carrying the label the board groups by. Everything below is about making
that arrangement reproducible from outside Trilium's own UI — there is no
custom TypeScript in our fork and this does not start any.

**Where a board's columns live.** The board view resolves its columns from
three places in order: the select options of the board's own `label:<groupBy>`
definition, then the columns saved in its `board.json` attachment, then the
values notes actually carry. Only the first is writable from out here, and it
is also the only one that can hold a column *nobody is standing in* — an empty
"Blocked" column exists because the definition says so. So the definition is
what this module writes, in the exact comma-separated form upstream's parser
reads (`promoted,alias=…,single,select,options=a;b;c`), and the board view
leaves it alone as long as the list already matches what it resolved.

**Why cards are keyed on a label and not a title.** The board is shared between
a person typing into Trilium and awm pushing work items in. A card awm owns
carries `#awmKey`; a card a person typed carries none, and nothing here will
ever match, move or overwrite it. Titles are not identity: awm has to be able
to rename a card it owns without orphaning it, and two people are free to title
two cards the same thing.

`#awmKey` is deliberately *not* promoted. Every promoted attribute is rendered
on the card face, and a bookkeeping key on every card would be noise on the one
surface whose whole job is to be scanned.
"""

from __future__ import annotations

from typing import Any

from awm.trilium import etapi

#: What a board groups by when nothing says otherwise, matching the board
#: view's own default (`DEFAULT_GROUP_BY` in the client).
DEFAULT_GROUP_BY = "status"

#: The columns a board is given when the caller names none. Four, because
#: "blocked" is the state a task board exists to make visible.
DEFAULT_COLUMNS = ("To do", "Doing", "Blocked", "Done")

#: The label that says a card is awm's to rewrite.
KEY_LABEL = "awmKey"


def _encode_option(option: str) -> str:
    """Upstream's `encodeOption`, exactly: `%` first so the escapes written
    after it are not escaped again."""
    return option.replace("%", "%25").replace(",", "%2C").replace(";", "%3B")


def _decode_option(option: str) -> str:
    """The exact inverse of :func:`_encode_option`: `%25` last, so a decoded
    `%` cannot start a triplet."""
    return option.replace("%2C", ",").replace("%3B", ";").replace("%25", "%")


def definition_value(columns: list[str], *, group_by: str = DEFAULT_GROUP_BY,
                     alias: str | None = None) -> str:
    """The `label:<groupBy>` attribute value that gives a board these columns.

    Promoted, because the column a card sits in is the point of the board and
    an unpromoted field never appears on the card at all — the status would
    then be reachable only by dragging.
    """
    parts = ["promoted"]
    label = alias if alias is not None else (
        "Status" if group_by == DEFAULT_GROUP_BY else group_by)
    if label:
        parts.append(f"alias={label}")
    parts += ["single", "select"]
    kept = [c.strip() for c in columns if c and c.strip()]
    if kept:
        parts.append("options=" + ";".join(_encode_option(c) for c in kept))
    return ",".join(parts)


def columns_of(client: etapi.Etapi, board_id: str,
               group_by: str = DEFAULT_GROUP_BY) -> list[str]:
    """The columns the board's own definition declares.

    Not the whole answer the board view computes — it also folds in the
    attachment and the values on notes, neither of which is reachable from
    here — but it is the half this service writes, and the only half that can
    name a column standing empty.
    """
    raw = client.label_value(board_id, f"label:{group_by}") or ""
    if "options=" not in raw:
        return []
    listed = raw.partition("options=")[2].split(",", 1)[0]
    return [_decode_option(c) for c in listed.split(";") if c]


def ensure(client: etapi.Etapi, *, title: str, parent: str = "root",
           columns: list[str] | None = None,
           group_by: str = DEFAULT_GROUP_BY) -> dict:
    """Create the board, or bring the one already there up to this shape.

    Idempotent on `(parent, title)` like `upsert_note`, and for the same
    reason: a board is a thing a person points at, so its title is how they
    named it. The body is left alone — a board note's content is the blurb
    above the columns, and a person may have written one.
    """
    # `None` and `[]` are different questions. Absent means "you choose";
    # empty means the caller computed a column list and it came out empty,
    # which is a board with nowhere to put a card, and collapsing the two
    # would answer the second by inventing four columns nobody asked for.
    wanted = list(DEFAULT_COLUMNS) if columns is None else columns
    cols = [c.strip() for c in wanted if c and c.strip()]
    if not cols:
        raise ValueError("a board needs at least one column")

    existing = [n for n in client.children(parent) if n.get("title") == title]
    if len(existing) > 1:
        raise etapi.EtapiError(
            f"{len(existing)} notes under {parent} are titled {title!r} — "
            f"refusing to guess which one is the board")

    if existing:
        note_id = existing[0]["noteId"]
        created = False
        if existing[0].get("type") != "book":
            client.patch_note(note_id, type="book")
    else:
        made = client.create_note(parent_note_id=parent, title=title,
                                  type="book", content="")
        note_id = made["note"]["noteId"]
        created = True

    changed = []
    for name, value in (
        ("viewType", "board"),
        ("board:groupBy", group_by),
        (f"label:{group_by}", definition_value(cols, group_by=group_by)),
    ):
        res = client.set_attribute(note_id=note_id, name=name, value=value)
        if res["changed"]:
            changed.append(name)

    return {"note_id": note_id, "created": created, "columns": cols,
            "group_by": group_by, "changed": changed}


def _cards(client: etapi.Etapi, board_id: str) -> dict[str, str]:
    """Every card under the board that awm owns, as `{awmKey: note_id}`.

    Searched rather than walked because the board groups its subtree
    recursively — a card two levels down is still on the board — and a walk
    would have to reproduce that traversal. `#awmKey` is exact and carries no
    user text, so it is safe in a query string in a way a title is not.
    """
    hits = client.search(f"#{KEY_LABEL}", ancestorNoteId=board_id, limit=1000,
                         fastSearch=False)
    out: dict[str, str] = {}
    for note in hits.get("results", []) or []:
        for a in note.get("attributes") or []:
            if a.get("type") == "label" and a.get("name") == KEY_LABEL:
                out.setdefault(a.get("value") or "", note["noteId"])
    out.pop("", None)
    return out


def card_upsert(client: etapi.Etapi, *, board: str, key: str, title: str,
                status: str, content: str | None = None,
                labels: dict[str, str] | None = None) -> dict:
    """Put a card on the board, or bring the one with this key up to date.

    Only cards carrying `#awmKey` are ever matched, so a card a person typed
    into the board by hand is invisible to this and survives every pass.
    """
    key = key.strip()
    if not key:
        raise ValueError("key is required: it is what makes the card awm's")
    if not title.strip():
        raise ValueError("title is required")

    known = _cards(client, board)
    note_id = known.get(key)
    changed: list[str] = []

    if note_id is None:
        made = client.create_note(parent_note_id=board, title=title.strip(),
                                  type="text", content=content or "")
        note_id = made["note"]["noteId"]
        client.set_attribute(note_id=note_id, name=KEY_LABEL, value=key)
        created = True
    else:
        created = False
        if client.note(note_id).get("title") != title.strip():
            client.patch_note(note_id, title=title.strip())
            changed.append("title")
        if content is not None and client.note_content(note_id) != content:
            client.set_content(note_id, content)
            changed.append("content")

    if client.set_attribute(note_id=note_id, name="status",
                            value=status)["changed"]:
        changed.append("status")
    for name, value in (labels or {}).items():
        if client.set_attribute(note_id=note_id, name=str(name).lstrip("#"),
                                value=str(value))["changed"]:
            changed.append(str(name))

    return {"note_id": note_id, "key": key, "board": board, "status": status,
            "created": created, "changed": changed}


def cards(client: etapi.Etapi, board_id: str) -> dict[str, Any]:
    """What is on the board, by column, both awm's cards and the hand-made ones.

    The board itself groups its whole subtree flattened, so this does too: a
    card is anything under the board carrying the grouping label, and its depth
    does not matter.
    """
    group_by = client.label_value(board_id, "board:groupBy") or DEFAULT_GROUP_BY
    hits = client.search(f"#{group_by}", ancestorNoteId=board_id, limit=1000,
                         fastSearch=False)
    by_column: dict[str, list[dict]] = {}
    for note in hits.get("results", []) or []:
        column, key = "", None
        for a in note.get("attributes") or []:
            if a.get("type") != "label":
                continue
            if a.get("name") == group_by:
                column = a.get("value") or ""
            elif a.get("name") == KEY_LABEL:
                key = a.get("value")
        by_column.setdefault(column, []).append(
            {"note_id": note["noteId"], "title": note.get("title"),
             "key": key, "owner": "awm" if key else "hand"})
    return {"board": board_id, "group_by": group_by,
            "columns": columns_of(client, board_id, group_by),
            "cards": by_column,
            "count": sum(len(v) for v in by_column.values())}
