"""Readable text for a Slack message whose content lives in ``blocks``.

Slack fills ``text`` with a placeholder ("This message contains interactive
elements.") when a message carries a block type it cannot flatten — a table, an
actions row — and with nothing at all for a bot message built only from legacy
``attachments``. :func:`slack_text` returns ``text`` when it is real, and
otherwise renders the blocks and attachments in the order Slack shows them.
"""

from __future__ import annotations

_PLACEHOLDERS = (
    "this message contains interactive elements",
    "this content can't be displayed",
)


def _is_placeholder(text: str) -> bool:
    t = text.strip().rstrip(".").lower()
    return not t or t in _PLACEHOLDERS


def _inline(el: dict) -> str:
    """One rich_text element (a run of text, a link, a mention) as plain text."""
    kind = el.get("type")
    if kind == "text":
        return el.get("text", "")
    if kind == "link":
        url, label = el.get("url", ""), el.get("text", "")
        return f"{label} ({url})" if label and label != url else url
    if kind == "user":
        return f"<@{el.get('user_id', '')}>"
    if kind == "usergroup":
        return f"<!subteam^{el.get('usergroup_id', '')}>"
    if kind == "channel":
        return f"<#{el.get('channel_id', '')}>"
    if kind == "emoji":
        return f":{el.get('name', '')}:"
    if kind == "broadcast":
        return f"@{el.get('range', 'here')}"
    if kind == "date":
        return el.get("fallback", "")
    if kind == "color":
        return el.get("value", "")
    return el.get("text", "")


def _runs(elements: list) -> str:
    return "".join(_inline(e) for e in elements if isinstance(e, dict))


def _rich(elements: list) -> list[str]:
    """The lines of a rich_text block's top-level elements."""
    lines: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        kind = el.get("type")
        sub = el.get("elements") or []
        if kind == "rich_text_section":
            lines.append(_runs(sub))
        elif kind == "rich_text_preformatted":
            lines.append("```\n" + _runs(sub) + "\n```")
        elif kind == "rich_text_quote":
            lines.extend("> " + ln for ln in _runs(sub).split("\n"))
        elif kind == "rich_text_list":
            ordered = el.get("style") == "ordered"
            pad = "  " * int(el.get("indent") or 0)
            for n, item in enumerate(sub, 1):
                mark = f"{n}." if ordered else "•"
                lines.append(f"{pad}{mark} {_runs(item.get('elements') or [])}")
    return lines


def _plain(obj) -> str:
    """A Slack text object (``{type, text}``) or a bare string."""
    if isinstance(obj, dict):
        return str(obj.get("text") or "")
    return str(obj or "")


def _cell(cell) -> str:
    if not isinstance(cell, dict):
        return _plain(cell)
    if cell.get("type") == "rich_text":
        return " ".join(_rich(cell.get("elements") or [])).strip()
    return _plain(cell)


def _table(block: dict) -> str:
    return "\n".join(" | ".join(_cell(c) for c in row)
                     for row in block.get("rows") or [])


def _block(block: dict) -> str:
    kind = block.get("type")
    if kind == "rich_text":
        return "\n".join(_rich(block.get("elements") or []))
    if kind == "table":
        return _table(block)
    if kind in ("section", "header"):
        parts = [_plain(block.get("text"))]
        parts.extend(_plain(f) for f in block.get("fields") or [])
        return "\n".join(p for p in parts if p)
    if kind == "markdown":
        return block.get("text", "")
    if kind == "context":
        return " ".join(
            p for p in (_plain(e) if e.get("type") != "image"
                        else e.get("alt_text", "")
                        for e in block.get("elements") or []
                        if isinstance(e, dict)) if p)
    if kind == "image":
        return _plain(block.get("title")) or block.get("alt_text", "")
    return ""


def _attachment(a: dict) -> str:
    """A legacy attachment's text; its ``fallback`` only when nothing else says more."""
    parts = [a.get("pretext", ""), a.get("title", ""), a.get("text", "")]
    parts.extend(f"{f.get('title', '')}: {f.get('value', '')}".strip(": ")
                 for f in a.get("fields") or [] if isinstance(f, dict))
    parts.extend(_block(b) for b in a.get("blocks") or [] if isinstance(b, dict))
    text = "\n".join(p for p in parts if p)
    return text or a.get("fallback", "")


def slack_text(m: dict) -> str:
    """The message's readable text.

    ``m["text"]`` wins when it is real. A table block is the exception: Slack's
    ``text`` never carries a table, so it is appended to a real ``text``.
    """
    text = m.get("text", "") or ""
    blocks = [b for b in m.get("blocks") or [] if isinstance(b, dict)]
    if not _is_placeholder(text):
        tables = [_table(b) for b in blocks if b.get("type") == "table"]
        return "\n".join([text, *[t for t in tables if t]])
    parts = [_block(b) for b in blocks]
    parts.extend(_attachment(a) for a in m.get("attachments") or []
                 if isinstance(a, dict))
    return "\n".join(p for p in parts if p).strip() or text
