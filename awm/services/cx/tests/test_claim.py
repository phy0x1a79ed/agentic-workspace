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
    monkeypatch.setattr(claim, "_type_command", lambda s, t: typed.append(t))
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
    monkeypatch.setattr(claim, "_type_command", lambda s, t: None)
    cleared = []
    monkeypatch.setattr(claim, "_abandon", lambda s: cleared.append(s.short))
    out = await claim.claim(TRUSTED)
    assert out["session"] is None
    assert cleared == ["warmfresh"]


def _moves(monkeypatch, box, claim):
    """Make `_type_command` do what `/cd` and `/rename` do, and record both."""
    typed = []

    def run(s, text):
        typed.append(text)
        rec = box / "jobs" / s.short / "state.json"
        data = json.loads(rec.read_text())
        if text.startswith("/cd "):
            data["cwd"] = data["originCwd"] = text[len("/cd "):]
        elif text.startswith("/rename "):
            data["name"] = text[len("/rename "):]
        rec.write_text(json.dumps(data))

    monkeypatch.setattr(claim, "_type_command", run)
    return typed


async def test_a_claim_renames_the_session_out_of_the_pool(
        box, trust_file, monkeypatch):
    """The whole point: the session stops advertising itself as a spare."""
    from awm.cx import claim, sessions

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    typed = _moves(monkeypatch, box, claim)
    out = await claim.claim(TRUSTED)
    assert out["session"] == "warmfresh"
    assert typed == [f"/cd {TRUSTED}", "/rename claimed dunlin"]
    s = {x.short: x for x in sessions.load()}["warmfresh"]
    assert s.name == "claimed dunlin"
    assert not sessions.is_ours(s)
    assert sessions.was_ours(s)


async def test_the_rename_comes_after_the_move_has_landed(
        box, trust_file, monkeypatch):
    """The recheck between them asks whether the name still carries the pool's
    prefix, and the rename is what takes it away."""
    from awm.cx import claim

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    typed = _moves(monkeypatch, box, claim)
    await claim.claim(TRUSTED)
    assert typed.index(f"/cd {TRUSTED}") < typed.index("/rename claimed dunlin")


async def test_a_rename_that_will_not_type_still_hands_the_session_over(
        box, trust_file, monkeypatch):
    """A session under the wrong name beats no session at all."""
    from awm import claudedaemon

    from awm.cx import claim

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    _moves(monkeypatch, box, claim)
    real = claim._type_command

    def refuse_rename(s, text):
        if text.startswith("/rename "):
            raise claudedaemon.DaemonError("no reachable PTY")
        real(s, text)

    monkeypatch.setattr(claim, "_type_command", refuse_rename)
    out = await claim.claim(TRUSTED)
    assert out == {"session": "warmfresh", "cwd": TRUSTED}


def test_the_claimed_name_keeps_the_pool_noun():
    from awm.cx import claim

    assert claim.claimed_name("<warm dunlin>", "abc123") == "claimed dunlin"
    assert claim.claimed_name("<warm >", "abc123") == "claimed abc123"
