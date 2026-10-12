"""Markdown for the generated notes: a task's Record and a node's Ledger."""

from __future__ import annotations

import time

from awm.reporting.ledger import COLUMNS, DONE

_CHECK = {"completed": "x", "in_progress": "~"}


def _at(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch)) + "Z" if epoch else ""


def _line(label: str, value: object) -> list[str]:
    return [f"- {label}: {value}"] if value else []


def render_record(task: dict, live: list[dict], removed: list[dict], events: list[dict]) -> str:
    where = "/".join(p for p in (task.get("project"), task.get("scope")) if p)
    meta = [
        *_line("peer", task["node"]), *_line("job", task["key"] if task["kind"] == "bg" else ""),
        *_line("session", task.get("session_id")), *_line("scope", where),
        *_line("model", task.get("model")), *_line("effort", task.get("effort")),
        *_line("state", COLUMNS.get(task["state"], task["state"])),
        *_line("waiting on", task.get("reason") if task["state"] in ("blocked", "review") else ""),
        *_line("ended", "the session died without a stop; resumable" if task.get("ended") else ""),
        *_line("started", _at(task["started"])), *_line("closed", _at(task.get("closed"))),
    ]
    todo = [f"- [{_CHECK.get(i['status'], ' ')}] {i['subject']}" for i in live] or ["(none)"]
    out = ["## Metadata", "", *meta, "", "## Plan", "", task.get("plan") or "(none)", "",
           "## Todo", "", *todo]
    if removed:
        out += ["", "### Removed", "", *[f"- {i['subject']} (removed {_at(i['removed_at'])})" for i in removed]]
    posts = [e for e in events if e["kind"] == "journal"]
    out += ["", "## Journal", ""]
    out += [f"### {_at(e['at'])}\n\n{e['detail']}\n" for e in posts] or ["(none)"]
    return "\n".join(out).rstrip() + "\n"


def render_ledger(node: str, tasks: list[dict]) -> str:
    rows = [f"- {_at(t['closed'])} {'done' if t['state'] == DONE else t['state']}: {t['title'] or t['key']}"
            + (f" ({t['project']}/{t['scope']})" if t.get("project") and t.get("scope") else "")
            for t in reversed(tasks)]
    return "\n".join([f"## Closed tasks on {node}", "", *(rows or ["(none)"])]) + "\n"
