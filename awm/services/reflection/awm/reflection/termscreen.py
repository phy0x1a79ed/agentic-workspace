"""What a terminal shows after a stream of its output, as text.

A background session's lane hands back every byte the host has sent since the
connection opened, not a rendered screen. A dialog that has been answered is
still in that stream, followed by the bytes that erased it, so substring-matching
the stream sees dialogs that closed seconds ago. Replaying the stream onto a grid
answers "what is on screen now" without a terminal-emulator dependency.

This is a small subset of a terminal, enough for a TUI that repaints by erasing
lines and moving the cursor: printable text, carriage return and line feed,
cursor moves (relative, absolute, by column and by row), erase in line and in
display, save and restore of the cursor, and a screen switch (alternate screen),
which starts a blank grid. Colours and every other sequence are dropped. Width is
one column per character and the grid never scrolls, so a screen taller than the
stream's own geometry keeps its rows rather than losing the top ones.
"""
from __future__ import annotations

import re

# A stream older than this is cut from the front. Cursor-relative paints read
# fine from any point, and a dialog is always among the last things drawn.
MAX_STREAM_CHARS = 400_000

_TOKEN = re.compile(
    r"\x1b\[(?P<params>[0-9;:?<>=! ]*)(?P<final>[@-~])"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP.*?\x1b\\"
    r"|\x1b[()][A-Za-z0-9]"
    r"|\x1b(?P<esc>[78MDE])"
    r"|\x1b."
    r"|(?P<ctl>[\r\n\b\t])"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    r"|(?P<text>[^\x00-\x1f\x7f]+)",
    re.S)

_SCREEN_SWITCH = {"?1049", "?1047", "?47"}


class _Grid:
    def __init__(self) -> None:
        self.rows: dict[int, list[str]] = {}
        self.row = 0
        self.col = 0
        self.saved = (0, 0)

    def clear(self) -> None:
        self.rows.clear()
        self.row = self.col = 0

    def write(self, text: str) -> None:
        line = self.rows.setdefault(self.row, [])
        if len(line) < self.col:
            line.extend(" " * (self.col - len(line)))
        for ch in text:
            if self.col < len(line):
                line[self.col] = ch
            else:
                line.append(ch)
            self.col += 1

    def erase_line(self, mode: int) -> None:
        line = self.rows.get(self.row)
        if line is None:
            return
        if mode == 0:
            del line[self.col:]
        elif mode == 1:
            for i in range(min(self.col + 1, len(line))):
                line[i] = " "
        else:
            line.clear()

    def erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            self.rows.clear()
        elif mode == 0:
            self.erase_line(0)
            for r in [r for r in self.rows if r > self.row]:
                del self.rows[r]
        elif mode == 1:
            self.erase_line(1)
            for r in [r for r in self.rows if r < self.row]:
                del self.rows[r]

    def erase_chars(self, n: int) -> None:
        line = self.rows.get(self.row)
        if line is not None:
            for i in range(self.col, min(self.col + n, len(line))):
                line[i] = " "

    def text(self) -> str:
        if not self.rows:
            return ""
        lines = ["".join(self.rows.get(r, [])).rstrip()
                 for r in range(max(self.rows) + 1)]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


def _numbers(params: str) -> list[int]:
    out = []
    for part in params.lstrip("?<>=! ").split(";"):
        part = part.split(":")[0]
        out.append(int(part) if part.isdigit() else 0)
    return out or [0]


def _csi(grid: _Grid, params: str, final: str) -> None:
    n = _numbers(params)
    first = n[0] or 1
    if final == "A":
        grid.row = max(0, grid.row - first)
    elif final == "B":
        grid.row += first
    elif final == "C":
        grid.col += first
    elif final == "D":
        grid.col = max(0, grid.col - first)
    elif final == "E":
        grid.row, grid.col = grid.row + first, 0
    elif final == "F":
        grid.row, grid.col = max(0, grid.row - first), 0
    elif final == "G":
        grid.col = first - 1
    elif final in "Hf":
        grid.row = (n[0] or 1) - 1
        grid.col = ((n[1] if len(n) > 1 else 0) or 1) - 1
    elif final == "d":
        grid.row = first - 1
    elif final == "K":
        grid.erase_line(n[0])
    elif final == "J":
        grid.erase_display(n[0])
    elif final == "X":
        grid.erase_chars(first)
    elif final == "s":
        grid.saved = (grid.row, grid.col)
    elif final == "u":
        grid.row, grid.col = grid.saved
    elif final in "hl" and params.startswith("?"):
        if any(f"?{v}" in _SCREEN_SWITCH for v in (str(x) for x in n)):
            grid.clear()


def render(stream: str) -> str:
    """The screen ``stream`` leaves behind, one text line per row."""
    grid = _Grid()
    for m in _TOKEN.finditer(stream[-MAX_STREAM_CHARS:]):
        if m.group("final"):
            _csi(grid, m.group("params"), m.group("final"))
        elif m.group("text"):
            grid.write(m.group("text"))
        elif m.group("esc"):
            esc = m.group("esc")
            if esc == "7":
                grid.saved = (grid.row, grid.col)
            elif esc == "8":
                grid.row, grid.col = grid.saved
            elif esc == "M":
                grid.row = max(0, grid.row - 1)
            elif esc == "D":
                grid.row += 1
            else:
                grid.row, grid.col = grid.row + 1, 0
        elif m.group("ctl"):
            ctl = m.group("ctl")
            if ctl == "\r":
                grid.col = 0
            elif ctl == "\n":
                grid.row, grid.col = grid.row + 1, 0
            elif ctl == "\b":
                grid.col = max(0, grid.col - 1)
            else:
                grid.col = (grid.col // 8 + 1) * 8
    return grid.text()
