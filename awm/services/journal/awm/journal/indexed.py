"""Which threads the journal sends to the search archive, and the text it sends.

One document stands for one thread: its first post, with each comment's text
after it, so a hit on a comment finds the post it answers (fleet.md R107 item 5).
"""

from __future__ import annotations

from typing import Any

from awm.journal import builtin_tags

#: A thread is indexed when its first post carries one of these tags.
INDEXED_TAGS = (builtin_tags.JOURNAL, builtin_tags.MESSAGE, builtin_tags.PRECEDENT)
_MARKS = ",".join("?" * len(INDEXED_TAGS))
#: A WHERE fragment over ``scope_posts`` keeping the thread first posts the archive holds.
INDEXED_SQL = (f" AND thread_id IS NULL AND EXISTS (SELECT 1 FROM post_tags it WHERE"
               f" it.post_id = scope_posts.id AND it.tag_id IN ({_MARKS}))"
               f" AND NOT EXISTS (SELECT 1 FROM post_tags ft WHERE"
               f" ft.post_id = scope_posts.id AND ft.tag_id = ?)")
INDEXED_PARAMS = [*INDEXED_TAGS, builtin_tags.FOLDED]


def may_hold(terms: Any) -> bool:
    """Whether a tag filter can match a thread the archive holds.

    A filter that requires a kind tag outside :data:`INDEXED_TAGS`, such as
    ``brief``, cannot, and its search falls back to a substring match.
    """
    from awm.journal import tags
    for negated, name in tags.parse_filter(terms):
        tag = tags.by_name(name)
        if (not negated and tag is not None and tag.id not in INDEXED_TAGS
                and (tag.id in builtin_tags.KIND_TAGS or tag.id.startswith(builtin_tags.MIGRATED_PREFIX))):
            return False
    return True


def thread_text(conn, root_id: str, body: str) -> str:
    """A thread's first post with each comment's text after it, oldest first."""
    parts = [body or ""]
    parts += [r["body"] for r in conn.execute(
        "SELECT body FROM scope_posts WHERE thread_id=? AND body != '' ORDER BY ts, id",
        (root_id,))]
    return "\n\n".join(p for p in parts if p)
