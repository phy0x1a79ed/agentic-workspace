"""Tests for the transcripts sweep.

Every test builds a whole fake `~/.claude/projects` tree and points the module
constants at it, because the defects this service exists to fix are all about
which paths the walk reaches — a test that mocks the filesystem would not have
caught either of them.
"""

from __future__ import annotations

import gzip
import os
import time

import pytest

from awm.transcripts import layout, sweep


@pytest.fixture
def tree(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    archive = tmp_path / "archive"
    projects.mkdir()
    archive.mkdir()
    monkeypatch.setattr(layout, "PROJECTS", projects)
    monkeypatch.setattr(layout, "ARCHIVE", archive)
    monkeypatch.setattr(sweep, "PROJECTS", projects)
    monkeypatch.setattr(sweep, "ARCHIVE", archive)
    return projects, archive


def make_session(projects, project, sid, *, age_days=0.0, sidecar=True,
                 transcript=True):
    p = projects / project
    p.mkdir(parents=True, exist_ok=True)
    old = time.time() - age_days * 86400
    if transcript:
        f = p / f"{sid}.jsonl"
        f.write_text('{"type":"user"}\n')
        os.utime(f, (old, old))
    if sidecar:
        d = p / sid / "tool-results"
        d.mkdir(parents=True)
        f = d / "out.txt"
        f.write_text("tool output")
        os.utime(f, (old, old))
        sub = p / sid / "subagents"
        sub.mkdir()
        f = sub / "a.jsonl"
        f.write_text('{"type":"assistant"}\n')
        os.utime(f, (old, old))
    return p


def test_sidecar_moves_with_its_transcript(tree):
    """The defect this service exists to fix."""
    projects, archive = tree
    make_session(projects, "proj", "sess", age_days=30)

    result = sweep.archive(days=7)

    assert result["sessions"] == 1
    assert not (projects / "proj" / "sess.jsonl").exists()
    assert not (projects / "proj" / "sess").exists()
    assert (archive / "proj" / "sess.jsonl.gz").exists()
    assert (archive / "proj" / "sess" / "tool-results" / "out.txt.gz").exists()
    assert (archive / "proj" / "sess" / "subagents" / "a.jsonl.gz").exists()


def test_memory_is_never_swept(tree):
    """A project's memory/ sits where a sidecar sits and must survive."""
    projects, _ = tree
    mem = projects / "proj" / "memory"
    mem.mkdir(parents=True)
    note = mem / "MEMORY.md"
    note.write_text("remembered")
    old = time.time() - 400 * 86400
    os.utime(note, (old, old))

    sweep.archive(days=7)

    assert note.exists()


def test_orphaned_sidecar_is_swept(tree):
    """221 of these existed on altair when the service was written."""
    projects, archive = tree
    make_session(projects, "proj", "orph", age_days=30, transcript=False)

    result = sweep.archive(days=7)

    assert result["orphans"] == 1
    assert not (projects / "proj" / "orph").exists()
    assert (archive / "proj" / "orph" / "tool-results" / "out.txt.gz").exists()


def test_a_recent_subagent_keeps_the_session_live(tree):
    """Age is the newest file anywhere in the session, not the transcript's."""
    projects, _ = tree
    p = make_session(projects, "proj", "sess", age_days=30)
    fresh = p / "sess" / "subagents" / "a.jsonl"
    os.utime(fresh, None)

    result = sweep.archive(days=7)

    assert result["sessions"] == 0
    assert (projects / "proj" / "sess.jsonl").exists()


def test_young_sessions_are_left_alone(tree):
    projects, _ = tree
    make_session(projects, "proj", "sess", age_days=1)
    assert sweep.archive(days=7)["sessions"] == 0
    assert (projects / "proj" / "sess.jsonl").exists()


def test_dry_run_moves_nothing(tree):
    projects, archive = tree
    make_session(projects, "proj", "sess", age_days=30)

    result = sweep.archive(days=7, dry_run=True)

    assert result["sessions"] == 1
    assert (projects / "proj" / "sess.jsonl").exists()
    assert not (archive / "proj").exists()


def test_archived_transcript_round_trips(tree):
    projects, archive = tree
    p = make_session(projects, "proj", "sess", age_days=30)
    original = (p / "sess.jsonl").read_text()

    sweep.archive(days=7)

    with gzip.open(archive / "proj" / "sess.jsonl.gz", "rt") as fh:
        assert fh.read() == original


def test_restore_puts_the_whole_session_back(tree):
    projects, _ = tree
    make_session(projects, "proj", "sess", age_days=30)
    sweep.archive(days=7)

    result = sweep.restore("proj", "sess")

    assert result["files"] == 3
    assert (projects / "proj" / "sess.jsonl").exists()
    assert (projects / "proj" / "sess" / "tool-results" / "out.txt").read_text() \
        == "tool output"


def test_restore_refuses_to_overwrite_a_live_session(tree):
    projects, _ = tree
    make_session(projects, "proj", "sess", age_days=30)
    sweep.archive(days=7)
    make_session(projects, "proj", "sess", age_days=0)

    with pytest.raises(FileExistsError):
        sweep.restore("proj", "sess")


def test_prune_defaults_to_reporting(tree):
    projects, archive = tree
    make_session(projects, "proj", "sess", age_days=400)
    sweep.archive(days=7)

    result = sweep.prune(days=180)

    assert result["sessions"] == 1
    assert result["dry_run"] is True
    assert (archive / "proj" / "sess.jsonl.gz").exists()


def test_prune_deletes_when_told_to(tree):
    projects, archive = tree
    make_session(projects, "proj", "sess", age_days=400)
    sweep.archive(days=7)

    result = sweep.prune(days=180, dry_run=False)

    assert result["sessions"] == 1
    assert not (archive / "proj" / "sess.jsonl.gz").exists()
    assert not (archive / "proj" / "sess").exists()


def test_a_session_a_process_still_holds_is_never_swept(tree, monkeypatch):
    """A background job parked for a fortnight is idle, not finished."""
    projects, _ = tree
    make_session(projects, "proj", "parked", age_days=30)
    monkeypatch.setattr(sweep.live, "session_ids", lambda: {"parked"})

    result = sweep.archive(days=7)

    assert result["sessions"] == 0
    assert (projects / "proj" / "parked.jsonl").exists()
