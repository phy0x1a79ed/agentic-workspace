"""A document's text cut into passages a hit can quote: exact spans of the text, each with its section.

A heading opens a section and starts a passage. Blank lines end a block. Blocks
of one section pack into a passage up to :data:`MAX_CHARS`, and a longer block
is cut at sentence ends, then at whitespace. ``text[start:end]`` is always the
passage itself, so a quote and its span never disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 700

_HEADING = re.compile(r"^(#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?\s*$)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Passage:
    start: int
    end: int
    section: str


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _blocks(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, section)`` per block, the heading line opening its section's first block."""
    out: list[tuple[int, int, str]] = []
    section, start, pos = "", None, 0

    def flush(stop: int) -> None:
        if start is not None:
            a, b = _trimmed(text, start, stop)
            if a < b:
                out.append((a, b, section))

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _HEADING.match(stripped):
            flush(pos)
            section, start = stripped.strip("#* :").strip(), pos
        elif not stripped:
            flush(pos)
            start = None
        elif start is None:
            start = pos
        pos += len(line)
    flush(pos)
    return out


def _cut(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """An over-long block as spans of at most :data:`MAX_CHARS`, cut at sentence ends where it can."""
    stops = [m.end() for m in _SENTENCE_END.finditer(text, start, end)] + [end]
    spans, a, last = [], start, start
    for b in stops:
        if b - a > MAX_CHARS and last > a:
            spans.append((a, last))
            a = last
        while b - a > MAX_CHARS:
            space = text.rfind(" ", a + 1, a + MAX_CHARS)
            stop = space if space > a else a + MAX_CHARS
            spans.append((a, stop))
            a = stop
        last = b
    if a < end:
        spans.append((a, end))
    return [s for s in (_trimmed(text, x, y) for x, y in spans) if s[0] < s[1]]


def split(text: str) -> list[Passage]:
    """The passages of ``text`` in order."""
    out: list[Passage] = []
    run: list[int] = []
    run_section = ""
    for start, end, section in _blocks(text):
        if run and (section != run_section or end - run[0] > MAX_CHARS):
            out.append(Passage(run[0], run[1], run_section))
            run = []
        if end - start > MAX_CHARS:
            out += [Passage(a, b, section) for a, b in _cut(text, start, end)]
            continue
        if not run:
            run, run_section = [start, end], section
        else:
            run[1] = end
    if run:
        out.append(Passage(run[0], run[1], run_section))
    return out


def section_at(passages: list[Passage], offset: int) -> Passage | None:
    """The passage holding ``offset``, else the last one before it."""
    best = None
    for p in passages:
        if p.start > offset:
            break
        best = p
    return best
