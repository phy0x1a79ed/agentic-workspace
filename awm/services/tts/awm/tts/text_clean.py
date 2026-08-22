"""Scrub markdown so piper doesn't pronounce the literal characters.

Claude's output is markdown-flavored: **bold**, `inline code`, ```fenced
blocks```, bullet lists with `- `, `# headings`, `[link](url)`, emoji,
etc. Piper's phonemizer happily reads those as "asterisk", "hyphen",
"open parenthesis" — unlistenable.

This module exposes ``clean_for_tts(text)`` which strips formatting
chars but keeps natural prose punctuation (``.,!?;:``) that piper uses
for prosody.

Heuristics are deliberately simple — this is a spike, not a full
markdown parser. If a particular pattern slips through, add a rule.
"""

from __future__ import annotations

import re


# Drop entire fenced code blocks — they read terribly aloud.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Inline code: keep the contents, drop the backticks.
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Bold/italic markers: **x**, __x__, *x*, _x_  →  x
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_]+)__")
_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_([^_\n]+)_(?!_)")

# Strikethrough ~~text~~  →  text
_STRIKE_RE = re.compile(r"~~([^~]+)~~")

# Heading markers at start of line: "## foo" → "foo"
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)

# Bullet markers at start of line: "- " / "* " / "+ " / "1. "
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+", re.MULTILINE)

# Blockquote markers: "> "
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)

# Markdown link: [text](url)  →  text
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# Bare URL → "link" (read as a single token rather than the whole URL)
_URL_RE = re.compile(r"https?://\S+")

# Drop residual stray formatting characters that survived the rules above.
# Conservative set — keep apostrophes, hyphens, normal punctuation.
_STRAY_RE = re.compile(r"[*_`~|<>{}\[\]]")

# Collapse runs of whitespace.
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n{2,}")

# Light emoji scrub — drop most non-BMP symbols and the common emoji block.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols, emoticons, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols / dingbats
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
