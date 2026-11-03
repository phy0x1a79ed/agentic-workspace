"""The scope brief: what a scope is for, one per scope, on the scope's channel (scopes.md R143).

A brief is a ``scope_posts`` row of kind ``brief``. A write appends a new post
naming the one it replaces, as a goal revision does, so the log keeps every
version and the newest is the brief in force.
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
    meta = post.meta if isinstance(post.meta, dict) else {}
    return Brief(id=post.id, project=post.project, scope=post.scope, author=post.author,
                 text=post.body or "", supersedes=str(meta.get("supersedes") or ""),
                 ts=post.ts)


def read(project: str, scope: str) -> Brief | None:
    row = JournalDAO().query_one(
        "SELECT * FROM scope_posts WHERE owner_project=? AND owner_scope=? AND kind=? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (project, scope, kinds.BRIEF))
    return _to_brief(channel._row_to_post(row)) if row else None


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
    meta = {"supersedes": prior.id} if prior is not None else {}
    return _to_brief(channel.post(project, scope, author=author, body=text,
                                  kind=kinds.BRIEF, meta=meta, origin=origin))


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
