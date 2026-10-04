"""Handing a session out: who gets one, who is told to launch cold."""

from __future__ import annotations

import json

import pytest

TRUSTED = "/home/tony/agentic_workspace/projects/awm/svc-cx"


@pytest.fixture
def trust_file(tmp_path, monkeypatch):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": {"/home/tony": {
        "hasTrustDialogAccepted": True}}}))
    monkeypatch.setenv("AWM_CX_TRUST_FILE", str(path))
    return path


def test_trust_is_inherited_from_an_ancestor(trust_file):
    from awm.cx import trust

    assert trust.trusted("/home/tony")
    assert trust.trusted("/home/tony/anything/at/all")


def test_a_directory_with_no_trusted_ancestor_is_untrusted(trust_file):
    from awm.cx import trust

    assert not trust.trusted("/tmp")


def test_an_unreadable_trust_file_reads_as_untrusted(monkeypatch, tmp_path):
    """Fails closed: the cost is a cold launch, never a dialog nobody answers."""
    from awm.cx import trust

    monkeypatch.setenv("AWM_CX_TRUST_FILE", str(tmp_path / "absent.json"))
    assert not trust.trusted("/home/tony")


async def test_an_untrusted_directory_is_refused_without_touching_a_session(
        box, trust_file, monkeypatch):
    """The dialog default is "No, stay put", and a session sitting on it
    answers the *next* claim's keystrokes instead of moving."""
    from awm.cx import claim

    typed = []
    monkeypatch.setattr(claim, "_type_cd", lambda s, w: typed.append(w))
    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    out = await claim.claim("/tmp")
    assert out["session"] is None
    assert "trust" in out["reason"]
    assert typed == []


async def test_a_claim_moves_the_newest_session_and_returns_it(
        box, trust_file, monkeypatch):
    from awm.cx import claim

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    _moves(monkeypatch, box, claim)
    out = await claim.claim(TRUSTED)
    assert out == {"session": "warmfresh", "cwd": TRUSTED}


async def test_two_claims_yield_one_session_and_one_miss(
        box, trust_file, monkeypatch):
    from awm.cx import claim

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    _moves(monkeypatch, box, claim)
    first = await claim.claim(TRUSTED)
    second = await claim.claim(TRUSTED)
    assert first["session"] == "warmfresh"
    assert second["session"] is None


async def test_a_session_that_would_not_move_is_left_idle(
        box, trust_file, monkeypatch):
    from awm.cx import claim

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    monkeypatch.setattr(claim, "MOVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(claim, "_type_cd", lambda s, w: None)
    cleared = []
    monkeypatch.setattr(claim, "_abandon", lambda s: cleared.append(s.short))
    out = await claim.claim(TRUSTED)
    assert out["session"] is None
    assert cleared == ["warmfresh"]


def _moves(monkeypatch, box, claim):
    """Make `_type_cd` do what a successful `/cd` does: move the record."""
    def move(s, want):
        rec = box / "jobs" / s.short / "state.json"
        data = json.loads(rec.read_text())
        data["cwd"] = want
        data["originCwd"] = want
        rec.write_text(json.dumps(data))

    monkeypatch.setattr(claim, "_type_cd", move)
