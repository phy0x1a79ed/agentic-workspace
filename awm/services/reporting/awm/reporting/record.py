"""The generated notes: a task's card body (HTML for a Trilium text note) and a node's Ledger (Markdown)."""

from __future__ import annotations

import html
import time

from markdown_it import MarkdownIt

from awm.reporting.ledger import COLUMNS, DONE, OPEN

TITLE_CHARS = 80

_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _at(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch)) + "Z" if epoch else ""


def cut_at_word(text: str, limit: int = TITLE_CHARS) -> str:
    """``text`` cut to ``limit`` characters at a word boundary when it has one."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    if text[limit] == " ":
        return head.rstrip()
    return head.rsplit(" ", 1)[0].rstrip() if " " in head else head


def plan_title(plan_text: str) -> str:
    """The text of the plan's first ``#`` heading, or an empty string."""
    tokens = _MD.parse(plan_text or "")
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag == "h1":
            return tokens[i + 1].content.strip()
    return ""


def markdown_html(text: str, *, shift: int, floor: int, drop_title: bool = False) -> str:
    """``text`` as HTML with every heading ``shift`` levels deeper, and none shallower than ``floor``.

    Raw HTML in the text is escaped. ``drop_title`` removes the first ``#`` heading.
    """
    tokens = _MD.parse(text or "")
    if drop_title:
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open" and tok.tag == "h1":
                del tokens[i:i + 3]
                break
    for tok in tokens:
        if tok.type in ("heading_open", "heading_close"):
            tok.tag = f"h{min(max(int(tok.tag[1]) + shift, floor), 6)}"
    return _MD.renderer.render(tokens, _MD.options, {}).strip()


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _line(label: str, value: object) -> list[str]:
    return [f"<li><strong>{label}:</strong> {_esc(value)}</li>"] if value else []


def _agent(task: dict) -> list[str]:
    """The session working an open task: its name, linked to its claude.ai page when it has one."""
    name, link = task.get("agent"), task.get("agent_link") or ""
    if not name or task["state"] not in OPEN or task.get("ended"):
        return []
    if link.startswith("https://"):
        value = f'<a href="{_esc(link)}">{_esc(name)}</a>'
    else:
        value = _esc(name) + (f" (<code>{_esc(link)}</code>)" if link else "")
    return [f"<li><strong>Agent:</strong> {value}</li>"]


def _block(summary: str, body: str) -> str:
    return f'<details class="trilium-collapsible"><summary>{_esc(summary)}</summary>\n{body}\n</details>'


def _checklist(live: list[dict]) -> str:
    items = []
    for item in live:
        subject = _esc(item["subject"])
        if item["status"] == "in_progress":
            subject = f"<strong>{subject}</strong> (in progress)"
        checked = ' checked="checked"' if item["status"] == "completed" else ""
        items.append(f'<li><label class="todo-list__label"><input type="checkbox" disabled="disabled"{checked}>'
                     f'<span class="todo-list__label__description">{subject}</span></label></li>')
    return '<ul class="todo-list">\n' + "\n".join(items) + "\n</ul>"


#: Ended details recorded before the detail named the cause, as their cards always showed them.
_LEGACY_ENDED = {"": "the session died without a stop; resumable",
                 "the session died without a stop": "the session died without a stop; resumable"}


def render_record(task: dict, live: list[dict], removed: list[dict], events: list[dict],
                  ended: str = "") -> str:
    """The card body: a status block, then Plan, Todo, Journal and, once an item is removed, Removed as closed collapsibles."""
    where = "/".join(p for p in (task.get("project"), task.get("scope")) if p)
    status = [
        *_line("State", COLUMNS.get(task["state"], task["state"])),
        *_agent(task),
        *_line("Scope", where), *_line("Peer", task["node"]),
        *_line("Job", task["key"] if task["kind"] == "bg" else ""),
        *_line("Session", task.get("session_id")),
        *_line("Model", task.get("model")), *_line("Effort", task.get("effort")),
        *_line("Waiting on", task.get("reason") if task["state"] in ("blocked", "review") else ""),
        *_line("Ended", _LEGACY_ENDED.get(ended, ended) if task.get("ended") else ""),
        *_line("Started", _at(task["started"])), *_line("Closed", _at(task.get("closed"))),
    ]
    done = sum(1 for i in live if i["status"] == "completed")
    active = sum(1 for i in live if i["status"] == "in_progress")
    if live:
        todo_head = f"Todo — {done}/{len(live)} done" + (f", {active} active" if active else "")
        todo_body = _checklist(live)
    else:
        todo_head, todo_body = "Todo — none", "<p>(none)</p>"
    posts = [e for e in events if e["kind"] == "journal"]
    entries = "\n".join(f"<h4>{_esc(_at(e['at']))}</h4>\n" + markdown_html(e["detail"], shift=4, floor=5)
                        for e in posts)
    gone = "\n".join(["<ul>", *[f"<li>{_esc(i['subject'])} (removed {_esc(_at(i['removed_at']))})</li>"
                                for i in removed], "</ul>"])
    plan = markdown_html(task.get("plan") or "", shift=1, floor=3, drop_title=True)
    parts = [
        "<ul>\n" + "\n".join(status) + "\n</ul>",
        _block("Plan", plan or "<p>(none)</p>"),
        _block(todo_head, todo_body),
        _block(f"Journal — {len(posts)} {'entry' if len(posts) == 1 else 'entries'}",
               entries or "<p>(none)</p>"),
    ]
    if removed:
        parts.append(_block(f"Removed — {len(removed)}", gone))
    return "\n<hr>\n".join(parts) + "\n"


def render_ledger(node: str, tasks: list[dict]) -> str:
    rows = [f"- {_at(t['closed'])} {'done' if t['state'] == DONE else t['state']}: {t['title'] or t['key']}"
            + (f" ({t['project']}/{t['scope']})" if t.get("project") and t.get("scope") else "")
            for t in reversed(tasks)]
    return "\n".join([f"## Closed tasks on {node}", "", *(rows or ["(none)"])]) + "\n"
