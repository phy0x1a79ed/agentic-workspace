"""Which journal channels a peer caller may not see, by the node's private marks.

A channel's owner pair names its project as a literal scope ``(<project>, <scope>)``,
a project-level goal channel ``('', 'project:<p>')``, or one of the legacy
spellings :func:`awm.config.privacy.hidden` knows (``task:<p>``, ``('', 'user:proj:<p>')``).
A mark on the project covers them all; :func:`exclusion` must match the same set.

A post tagged with a marked project's name, ignoring case, is hidden wherever it
sits, as precedence hid a decision with such a tag (fleet.md R107 item 9). A tag
only ever hides: none exposes a post.
"""

from __future__ import annotations

from awm.config import privacy

_PROJECT_CHANNEL = "project:"

NOTHING = (" AND 0", [])
EVERYTHING = ("", [])


def owner_hidden(owner_project: str | None, owner_scope: str | None) -> bool:
    """Whether a mark covers the channel ``(owner_project, owner_scope)``."""
    if not owner_project and (owner_scope or "").startswith(_PROJECT_CHANNEL):
        return privacy.hidden(owner_scope[len(_PROJECT_CHANNEL):])
    return privacy.hidden(owner_project or "", owner_scope or None)


def hides(as_: str | None, owner_project: str | None, owner_scope: str | None) -> bool:
    """Whether the caller ``as_`` may not see the channel."""
    return privacy.from_peer(as_) and owner_hidden(owner_project, owner_scope)


def _name(text: str) -> str:
    return " ".join(str(text).split()).lower()


def hides_post(as_: str | None, post) -> bool:
    """Whether the caller ``as_`` may not see ``post``: by its channel or by a marked tag."""
    if not privacy.from_peer(as_):
        return False
    if owner_hidden(post.project, post.scope):
        return True
    try:
        names = {_name(m["project"]) for m in privacy.marks()}
    except privacy.MarksUnreadable:
        return bool(post.tags)
    return any(_name(t) in names for t in post.tags)


def exclusion(as_: str | None) -> tuple[str, list]:
    """A ``scope_posts`` WHERE fragment, ``(" AND …", params)``, dropping what ``as_`` may not see."""
    if not privacy.from_peer(as_):
        return EVERYTHING
    try:
        marks = privacy.marks()
    except privacy.MarksUnreadable:
        return NOTHING
    clauses, params = [], []
    names = sorted({_name(m["project"]) for m in marks})
    if names:
        clauses.append("EXISTS (SELECT 1 FROM post_tags xt JOIN tags xn ON xn.id = xt.tag_id"
                       " WHERE xt.post_id = scope_posts.id AND lower(xn.name) IN ("
                       + ",".join("?" * len(names)) + "))")
        params += names
    for m in marks:
        project = m["project"]
        if m["scope"] is None:
            clauses.append("owner_project IN (?, ?)"
                           " OR (owner_project = '' AND owner_scope IN (?, ?))")
            params += [project, "task:" + project, _PROJECT_CHANNEL + project, "user:proj:" + project]
        else:
            prefix = m["scope"] + "/"
            clauses.append("owner_project IN (?, ?)"
                           " AND (owner_scope = ? OR substr(owner_scope, 1, ?) = ?)")
            params += [project, "task:" + project, m["scope"], len(prefix), prefix]
    if not clauses:
        return EVERYTHING
    return " AND NOT (" + " OR ".join(f"({c})" for c in clauses) + ")", params
