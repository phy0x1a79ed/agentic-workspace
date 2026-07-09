"""On-disk file store for note bodies.

Each note's markdown lives in ``AWM_DIR/services/notes/files/<uuid>.md`` — the
canonical content (the DB only indexes it). The uuid filename is stable across
retitles, so the on-disk path the editor shows never changes when you rename a
note. Reads are tolerant of a missing file (returns ``""``) so a DB row whose
file was removed out-of-band still resolves.
"""

from __future__ import annotations

from pathlib import Path

from . import config


def file_path(note_id: str) -> Path:
    return config.files_dir() / f"{note_id}.md"


def read(note_id: str) -> str:
    p = file_path(note_id)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def write(note_id: str, content: str) -> Path:
    p = file_path(note_id)
    # Atomic-ish: write a temp sibling then replace, so a crash mid-write never
    # leaves a half-written note.
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return p


def remove(note_id: str) -> None:
    p = file_path(note_id)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
