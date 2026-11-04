"""The history render: a scope's journal entries and its project's, as markdown.

Every session's startup ritual reads this render, so its layout is a contract.
"""

from __future__ import annotations

import re
from awm.journal import builtin_tags, inbox, private
from awm.journal.channel import _coerce_meta
from awm.journal.dao import JournalDAO
from awm.journal.refs import validate_project, validate_scope

_TAG_RUN_RE = re.compile(r"<[^>\n]*>")


def _neutralise_title(text: str) -> str:
    """Make an arbitrary string safe to render as a one-line markdown title.

    A journal post with no ``meta.title`` falls back to its raw body, which can
    contain newlines and — when a tool call was mangled in transport — literal
    tool-call tag fragments (``</invoke>``, ``<meta>…</meta>``). Strip tag-like
    runs and collapse whitespace so neither can smuggle markup or break the line
    in the render. Presentation-only; the stored row is intact.
    """
    text = _TAG_RUN_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:80] + "…") if len(text) > 80 else text


# How many of a scope's OWN journal entries always survive into the render, and
# how many project-wide entries share the rest of it. The floor is what
# stops a busy project from crowding a scope out of its own history.
_OWN_JOURNAL_FLOOR = 10
_PROJECT_JOURNAL_WINDOW = 15


def generate_history_md(project: str, scope: str, exclude: tuple[str, list] = ("", [])) -> str:
    """The history text for ``scope``: its own journal entries, then its project's.

    ``exclude`` is a WHERE fragment from :func:`awm.journal.private.exclusion`.
    """
    hide, hide_params = exclude
    preface = f"# Project History: {project}\n\n"
    unread = inbox.unread_count(project, scope, exclude=exclude)
    if unread:
        preface += (
            f"**Inbox:** {unread} unread message{'s' if unread != 1 else ''} at render time. "
            f"Read the digest with `journal(verb=\"inbox\", args={{project:\"{project}\", "
            f"scope:\"{scope}\"}})`.\n\n")

    dao = JournalDAO()
    # Journal entries are scope_posts tagged journal (a scope IS the channel; the
    # debrief is a self-post). Structured fields live in meta.
    hide = (" AND EXISTS (SELECT 1 FROM post_tags jt WHERE jt.post_id = scope_posts.id"
            " AND jt.tag_id = ?)" + hide)
    hide_params = [builtin_tags.JOURNAL, *hide_params]
    #
    # TWO queries, not one. A single project-wide SELECT ... LIMIT ranks a
    # scope's own history against every sibling's, so in a busy project a scope
    # opens the file the startup ritual sent it to and finds none of its own
    # work — it then re-derives what a past session already proved. Measured on
    # awm/svc-scopes: its four entries ranked 62nd, 67th, 68th and 88th of 95.
    own = dao.query_all(
        "SELECT id, owner_scope, body, meta, ts FROM scope_posts "
        f"WHERE owner_project=? AND owner_scope=? {hide} "
        "ORDER BY ts DESC LIMIT ?",
        (project, scope, *hide_params, _OWN_JOURNAL_FLOOR),
    )
    own_total = dao.query_all(
        "SELECT COUNT(*) AS n FROM scope_posts "
        f"WHERE owner_project=? AND owner_scope=? {hide}",
        (project, scope, *hide_params),
    )[0]["n"]
    others = dao.query_all(
        "SELECT id, owner_scope, body, meta, ts FROM scope_posts "
        f"WHERE owner_project=? AND owner_scope!=? {hide} "
        "ORDER BY ts DESC LIMIT ?",
        (project, scope, *hide_params, _PROJECT_JOURNAL_WINDOW),
    )
    others_total = dao.query_all(
        "SELECT COUNT(*) AS n FROM scope_posts "
        f"WHERE owner_project=? AND owner_scope!=? {hide}",
        (project, scope, *hide_params),
    )[0]["n"]

    def _parse(row):
        meta = _coerce_meta(row["meta"])
        body = row["body"] or ""
        title = _neutralise_title(meta.get("title") or body)
        return meta, title

    def _entry(row, *, tag_scope: bool) -> list[str]:
        meta, title = _parse(row)
        outcome = f" [{meta['outcome']}]" if meta.get("outcome") else ""
        tag = f" ({row['owner_scope']})" if tag_scope else ""
        out = [f"**[{row['id']}] {title}**{outcome}{tag}"]
        if meta.get("deviations"):
            out.append(f"- Deviations: {meta['deviations']}")
        if meta.get("suggestions"):
            out.append(f"- Suggestions: {meta['suggestions']}")
        out.append("")
        return out

    def _omitted(shown: int, total: int) -> str | None:
        """The line that stops a truncated file from looking complete."""
        n = total - shown
        if n <= 0:
            return None
        return (f"*+{n} older not shown — `journal(verb=\"fetch\")` with project={project} "
                f"tags=journal to read them.*\n")

    sections = []
    if own:
        lines = [f"## This Scope's Journal ({scope})\n"]
        for row in own:
            lines.extend(_entry(row, tag_scope=False))
        tail = _omitted(len(own), own_total)
        if tail:
            lines.append(tail)
        sections.append("\n".join(lines))

    if others:
        lines = ["## Sibling Scopes' Journal\n"]
        for row in others:
            lines.extend(_entry(row, tag_scope=True))
        tail = _omitted(len(others), others_total)
        if tail:
            lines.append(tail)
        sections.append("\n".join(lines))

    if not own and not others:
        sections.append(
            "*No journal entries yet. They appear here after agents post them "
            "via `journal(verb=\"post\")` with tags=[journal].*\n"
        )

    return preface + "\n".join(sections)


def render(project: str, scope: str, *, as_: str | None = None) -> dict:
    """Render ``scope``'s history as the caller ``as_`` may see it."""
    validate_project(project)
    validate_scope(scope)
    content = generate_history_md(project, scope, private.exclusion(as_))
    return {"content": content, "history_lines": len(content.splitlines())}
