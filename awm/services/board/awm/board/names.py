"""Names the board stores in labels, and how it cleans what a caller says about itself."""

from __future__ import annotations

import re

#: Longest agent name the board keeps.
MAX_AGENT = 100

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def clean_agent(value) -> str | None:
    """A session name as the board keeps it: one line of text, or None. Never an error."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = "".join(ch for ch in text if ch.isprintable())[:MAX_AGENT].strip()
    return text or None


def clean_node(value) -> str | None:
    return value if isinstance(value, str) and _NODE.match(value) else None


def owner_label(swarm: str, node: str | None, agent: str | None) -> str:
    """``<swarm>/<node>/<agent>``, left short when the tail is unknown."""
    if not (node or agent):
        return swarm
    tail = (agent or "").replace("/", "-")
    return f"{swarm}/{node or '-'}" + (f"/{tail}" if tail else "")


def check_project(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _PROJECT.match(value):
        raise ValueError(f"project must be a lowercase slug: {value!r}")
    return value
