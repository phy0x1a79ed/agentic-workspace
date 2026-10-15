"""The card states, and how the previous release's four statuses map onto them.

A card's state is the column folder it sits in, and the folders mirror the Swarm
board. The wire still carries the old four-value `status` on every card so a
peer on the previous release keeps parsing what it reads, and the filters still
accept it.
"""

from __future__ import annotations

STATES = ("proposed", "planned", "active", "blocked", "review", "done", "rejected")

#: State to the title of its column folder.
TITLES = {"proposed": "Proposed", "planned": "Planned", "active": "Active",
          "blocked": "Blocked", "review": "Pending review", "done": "Done",
          "rejected": "Rejected"}

#: The states of a card somebody holds.
HELD = ("planned", "active", "blocked", "review")
#: The states the claimant may set with `board_status`.
SETTABLE = HELD + ("done",)
TERMINAL = ("done", "rejected")

#: The previous release's vocabulary, still sent as `status` and still accepted as a filter.
STATUSES = ("posted", "in_progress", "done", "failed")

STATUS_OF = {"proposed": "posted", "planned": "in_progress", "active": "in_progress",
             "blocked": "in_progress", "review": "in_progress", "done": "done",
             "rejected": "failed"}

#: Old status to the states it stands for, so `status=in_progress` matches every held card.
STATES_OF = {"posted": ("proposed",), "in_progress": HELD, "done": ("done",),
             "failed": ("rejected",)}

#: The column an old release's `#status` label names, to the state a card in it is in.
_FROM_LABEL = {"posted": "proposed", "in progress": "planned", "done": "done",
               "failed": "rejected"}


def states_for(value: str) -> tuple[str, ...]:
    """The states a `status` or `state` filter value matches. Old and new spellings both work."""
    if value in STATES:
        return (value,)
    if value in STATES_OF:
        return STATES_OF[value]
    raise ValueError(f"status must be one of {STATUSES} or a state in {STATES}: {value!r}")


def from_label(label: str | None) -> str | None:
    """The state an old `#status` column name stands for, or None for any other name."""
    return _FROM_LABEL.get((label or "").strip().lower())


def from_recorded(value: str | None) -> str:
    """A state read back from the watcher's memory, which an older release wrote as a status."""
    if value in STATES:
        return value
    return {"posted": "proposed", "in_progress": "planned", "failed": "rejected"}.get(
        value or "", "proposed")
