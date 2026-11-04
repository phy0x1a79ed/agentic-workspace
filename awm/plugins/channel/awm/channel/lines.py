"""What a holder sees: one capped line per message, and a transcript to grep."""

from __future__ import annotations

import time
from pathlib import Path

#: The most characters of a message a pushed line carries (R110 item 6).
TEXT_MAX = 1500


def push_line(*, kind: str, name: str, trust: str, who: str, text: str,
              seq: int, transcript: Path) -> str:
    """``<kind> <name> [<trust>] from <who>: <text>`` on one line.

    Text past :data:`TEXT_MAX` is cut, and the line names the transcript and the
    message number that hold all of it.
    """
    flat = " ⏎ ".join(text.splitlines()) if text else ""
    if len(flat) > TEXT_MAX:
        flat = (flat[:TEXT_MAX]
                + f" …(cut at {TEXT_MAX} characters, message {seq} in full: {transcript})")
    return f"{kind} {name} [{trust}] from {who}: {flat}"


def notice(*, kind: str, name: str, text: str) -> str:
    """A line from the channel itself, not from the other side."""
    return f"{kind} {name}: {text}"


class Transcripts:
    """One plain-text file per channel. Each message starts with a header line,
    ``--- <seq> <by> <who> <time>``, then its text exactly as it came."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / f"{name}.txt"

    def append(self, name: str, *, seq: int, by: str, who: str, text: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        path = self.path(name)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with path.open("a", encoding="utf-8") as f:
            f.write(f"--- {seq} {by} {who} {stamp}\n{text}\n")
        return path
