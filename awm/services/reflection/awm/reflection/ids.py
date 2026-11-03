"""The one session id of `sessions.md` R9: two hex characters naming the platform, then the platform's own id as hex.

A Claude Code session is ``cc`` plus its session UUID with the dashes removed.
A short form is only a prefix lookup on this id, never an id of its own.
"""
from __future__ import annotations

import re

PLATFORM_CLAUDE = "cc"
PLATFORM_DEEPSEEK = "ds"
PLATFORMS = (PLATFORM_CLAUDE, PLATFORM_DEEPSEEK)

_HEX = re.compile(r"^[0-9a-f]+$")
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")


class BadSessionId(ValueError):
    """The text is not a session id in any form awm accepts."""


def r9(platform: str, raw: str) -> str:
    """The R9 id of the platform's own session id ``raw``."""
    if platform not in PLATFORMS:
        raise BadSessionId(f"unknown platform code {platform!r}; known: {', '.join(PLATFORMS)}")
    digits = (raw or "").strip().lower().replace("-", "")
    if not digits or not _HEX.match(digits):
        raise BadSessionId(f"{raw!r} is not a hex session id")
    return platform + digits


def parse_r9(session_id: str) -> tuple[str, str]:
    """``(platform, raw)`` for an R9 id. ``raw`` is the platform's own form: a dashed UUID for Claude Code."""
    text = (session_id or "").strip().lower()
    platform, digits = text[:2], text[2:]
    if platform not in PLATFORMS or not digits or not _HEX.match(digits):
        raise BadSessionId(f"{session_id!r} is not an R9 session id")
    if platform == PLATFORM_CLAUDE and len(digits) == 32:
        return platform, "-".join((digits[:8], digits[8:12], digits[12:16],
                                   digits[16:20], digits[20:]))
    return platform, digits


def normalise(ref: str) -> str:
    """The R9 id for ``ref``: an R9 id as is, or a bare Claude session UUID.

    A job id or a name is not a session id. Resolve it through the harness first.
    """
    text = (ref or "").strip().lower()
    if _UUID.match(text):
        return r9(PLATFORM_CLAUDE, text)
    parse_r9(text)
    return text


def is_r9(ref: str) -> bool:
    try:
        parse_r9(ref)
    except BadSessionId:
        return False
    return True
