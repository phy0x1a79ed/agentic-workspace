"""Emoji names for reactions: Slack takes a name, Discord takes the character.

``react`` accepts either form (``👀``, ``eyes`` or ``:eyes:``). The table covers
the common acknowledgements. A name outside it passes through unchanged, so a
workspace's custom emoji still reaches Slack by name.
"""

from __future__ import annotations

_BY_NAME = {
    "eyes": "👀",
    "+1": "👍",
    "thumbsup": "👍",
    "white_check_mark": "✅",
    "heart": "❤️",
    "tada": "🎉",
    "pray": "🙏",
    "x": "❌",
    "rocket": "🚀",
    "thinking_face": "🤔",
    "joy": "😂",
    "wave": "👋",
}
_BY_CHAR = {}
for _name, _char in _BY_NAME.items():
    _BY_CHAR.setdefault(_char, _name)
    _BY_CHAR.setdefault(_char.rstrip("️"), _name)


def _bare(emoji: str) -> str:
    return emoji.strip().strip(":")


def to_char(emoji: str) -> str | None:
    """The character for ``emoji``, or None for a name the table lacks."""
    e = emoji.strip()
    if e in _BY_CHAR or (e and not e.isascii()):
        return e
    return _BY_NAME.get(_bare(e).lower())


def to_name(emoji: str) -> str:
    """The Slack name for ``emoji``. An unknown name passes through bare."""
    e = emoji.strip()
    return _BY_CHAR.get(e) or _BY_CHAR.get(e.rstrip("️")) or _bare(e)
