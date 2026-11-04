"""The scope brief: what a scope is for, one per scope, on the scope's channel (scopes.md R143).

A brief is a ``scope_posts`` row tagged ``brief``. A write appends a new post that
replies to the one it replaces, which gets the ``superseded`` tag, as a goal
revision does (fleet.md R107 item 8). The thread keeps every version, and the
brief in force is the newest one not superseded.
"""

from __future__ import annotations

from dataclasses import dataclass

from awm.journal import channel, kinds
from awm.journal.dao import JournalDAO

MAX_CHARS = 400


class BriefError(ValueError):
    """A brief could not be written as asked."""


@dataclass
class Brief:
    id: str
    project: str
    scope: str
    author: str
    text: str
    supersedes: str
    ts: str

    def to_dict(self) -> dict:
        return {"id": self.id, "project": self.project, "scope": self.scope,
                "author": self.author, "text": self.text,
                "supersedes": self.supersedes, "ts": self.ts}


def _to_brief(post: channel.ScopePost) -> Brief:
    return Brief(id=post.id, project=post.project, scope=post.scope, author=post.author,
                 text=post.body or "", supersedes=post.reply_to or "", ts=post.ts)


def _tagged(tag_id: str, negated: bool = False) -> str:
    return (f" AND {'NOT ' if negated else ''}EXISTS (SELECT 1 FROM post_tags bt"
            f" WHERE bt.post_id = scope_posts.id AND bt.tag_id = '{tag_id}')")


def read(project: str, scope: str) -> Brief | None:
    row = JournalDAO().query_one(
        "SELECT * FROM scope_posts WHERE owner_project=? AND owner_scope=?"
        f"{_tagged(kinds.BRIEF)}{_tagged(kinds.SUPERSEDED, True)}"
        " ORDER BY ts DESC, rowid DESC LIMIT 1", (project, scope))
    return _to_brief(channel._row_to_post(row)) if row else None


def history(project: str, scope: str) -> list[Brief]:
    """Every version of the scope's brief, oldest first, by walking the reply links up."""
    brief = read(project, scope)
    out, seen = [], set()
    while brief is not None and brief.id not in seen:
        seen.add(brief.id)
        out.append(brief)
        parent = channel.get_post(brief.supersedes) if brief.supersedes else None
        brief = _to_brief(parent) if parent is not None and parent.has(kinds.BRIEF) else None
    return out[::-1]


def _checked(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise BriefError("a brief needs text")
    if len(text) > MAX_CHARS:
        raise BriefError(f"the brief is {len(text)} characters; the limit is {MAX_CHARS}. "
                         "Say only what the scope is for, in at most two sentences.")
    return text


def set_brief(project: str, scope: str, text: str, *, author: str,
              origin: str | None = None) -> Brief:
    text = _checked(text)
    prior = read(project, scope)
    if prior is not None and prior.text == text:
        return prior
    post = channel.post(project, scope, author=author, body=text, extra_tags=[kinds.BRIEF],
                        reply_to=prior.id if prior is not None else None, origin=origin)
    if prior is not None:
        channel.mark(prior.id, kinds.SUPERSEDED)
    return _to_brief(post)


def edit_brief(project: str, scope: str, old: str, new: str, *, author: str,
               origin: str | None = None) -> Brief:
    """Replace the one passage ``old`` of the brief in force with ``new``."""
    prior = read(project, scope)
    if prior is None:
        raise BriefError(f"{project}/{scope} has no brief to edit; use brief_set")
    if not old:
        raise BriefError("old must name the passage to replace")
    count = prior.text.count(old)
    if count != 1:
        raise BriefError(f"old occurs {count} times in the brief; it must occur exactly once")
    return set_brief(project, scope, prior.text.replace(old, new, 1),
                     author=author, origin=origin)
