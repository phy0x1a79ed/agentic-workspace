"""Scrub markdown so TTS doesn't pronounce literal formatting chars."""

from __future__ import annotations

import re


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_]+)__")
_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_([^_\n]+)_(?!_)")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")
_STRAY_RE = re.compile(r"[*_`~|<>{}\[\]]")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n{2,}")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def clean_for_tts(text: str) -> str:
    if not text:
        return text
    t = text
    t = _CODE_FENCE_RE.sub(" ", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _BOLD_RE.sub(r"\1", t)
    t = _BOLD_UNDER_RE.sub(r"\1", t)
    t = _ITALIC_STAR_RE.sub(r"\1", t)
    t = _ITALIC_UNDER_RE.sub(r"\1", t)
    t = _STRIKE_RE.sub(r"\1", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub("link", t)
    t = _HEADING_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _BLOCKQUOTE_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = _STRAY_RE.sub("", t)
    t = _BLANK_LINE_RE.sub(". ", t)
    t = _WHITESPACE_RE.sub(" ", t)
    return t.strip()
