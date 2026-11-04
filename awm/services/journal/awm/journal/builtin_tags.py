"""The tags the journal's code names, by fixed id (fleet.md R107 items 6 to 9).

Kinds folded into tags: message, journal and brief are tags with the ids
below. The schema seeds these rows, so this module imports nothing of the journal.
"""

from __future__ import annotations

MESSAGE = "builtin:message"
JOURNAL = "builtin:journal"
BRIEF = "builtin:brief"
CLOSED = "builtin:closed"
SUPERSEDED = "builtin:superseded"
PRECEDENT = "builtin:precedent"
FOLDED = "builtin:folded"

BUILTINS: dict[str, tuple[str, str]] = {
    MESSAGE: ("message", "A post by a user or by another scope or agent."),
    JOURNAL: ("journal", "A scope's own session log, the debrief entry."),
    BRIEF: ("brief", "What a scope is for within its project."),
    CLOSED: ("closed", "Done with, like a closed issue. A post without it is open."),
    SUPERSEDED: ("superseded", "A later post that replies to this one replaces it."),
    PRECEDENT: ("precedent", "A decision Tony made or overrode that holds beyond the session; "
                             "its body holds the context, the question and the decision."),
    FOLDED: ("folded", "An addendum folded into the post it corrects, as a new version of that post. "
                       "Reads skip it unless the tag filter names it."),
}

#: The tags that stand for what ``kind`` said.
KIND_TAGS = (MESSAGE, JOURNAL, BRIEF)

#: ``kind`` strings the code names, and the tag each one means.
KIND_TO_TAG = {"message": MESSAGE, "journal": JOURNAL, "brief": BRIEF,
               "precedent": PRECEDENT}

MIGRATED_PREFIX = "migrated:"
MIGRATED_DESCRIPTION = "A post kind migrated from the old kind column."


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def builtins_sql() -> str:
    """INSERT statements for the fixed tags: the fresh schema, the migration and every start."""
    return "".join(
        "INSERT OR IGNORE INTO tags (id, name, description, origin, version, created_at,"
        f" updated_at) VALUES ({_q(i)}, {_q(n)}, {_q(d)}, '', 1, 0, 0);\n"
        for i, (n, d) in BUILTINS.items())


#: Kinds that fold into another kind's tag: the goal log went (fleet.md R107 item 10),
#: and each real goal is an ordinary journal post now. ``debrief`` and ``note`` are
#: the old names of a journal and a message.
FOLDED_KINDS = {"goal": JOURNAL, "debrief": JOURNAL, "note": MESSAGE}


def known_kinds_sql() -> str:
    """The kinds with a fixed tag, as an SQL list."""
    return ", ".join(_q(k) for k in (*KIND_TO_TAG, *FOLDED_KINDS))


def kind_case_sql(column: str) -> str:
    """A CASE expression mapping a kind column to its fixed tag id, NULL for any other kind."""
    whens = " ".join(f"WHEN {_q(k)} THEN {_q(t)}"
                     for k, t in {**KIND_TO_TAG, **FOLDED_KINDS}.items())
    return f"CASE {column} {whens} ELSE NULL END"
