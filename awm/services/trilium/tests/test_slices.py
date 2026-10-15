"""Minting, listing, revoking and resolving slice links.

A slice opens one note and its descendants to somebody with no awm account.
These tests hold down the two things another team's work depends on directly:
`slice_resolve`'s answer shape, and that an unknown, revoked or expired token
all resolve to the exact same "not found" -- the edge turns that into a 404
and a dead link must be indistinguishable from one that never existed.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _never_the_real_vault(monkeypatch):
    def _refuse(*a, **kw):
        raise AssertionError(
            "a test reached the live vault — use the `vault` fixture")
    monkeypatch.setattr(etapi.httpx, "request", _refuse)


@pytest.fixture(autouse=True)
def _fixed_edge(monkeypatch):
    """A deterministic public host, so the printed URL is assertable."""
    monkeypatch.setattr(hub_adapter.config, "edge_url",
                        lambda: "https://vault.example.org:8443")


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    return fake


def call(verb: str, **args):
    """A verb as the console reaches it: `as_` is None, so the gate admits."""
    return asyncio.run(hub_adapter.HANDLERS[verb](args, None))


def _note(vault) -> str:
    return call("note_create", title="Paper")["note_id"]


# -- minting ------------------------------------------------------------


def test_minting_a_bound_token(vault):
    note_id = _note(vault)
    out = call("slice_expose", note_id=note_id, user="steven")
    assert out["note_id"] == note_id
    assert out["user"] == "steven"
    assert out["write"] is False
    assert "/" not in out["token"]
    assert out["url"] == (
        f"https://vault.example.org:8443/slice/{out['token']}/"
        f"?user=steven#root/{note_id}")


def test_minting_an_open_token(vault):
    note_id = _note(vault)
    out = call("slice_expose", note_id=note_id)
    assert out["user"] is None
    assert out["url"] == (
        f"https://vault.example.org:8443/slice/{out['token']}/#root/{note_id}")


def test_a_writable_slice_is_recorded_as_such(vault):
    note_id = _note(vault)
    out = call("slice_expose", note_id=note_id, user="steven", write=True)
    assert out["write"] is True


def test_exposing_a_note_marks_it_sliced(vault):
    note_id = _note(vault)
    call("slice_expose", note_id=note_id, user="steven")
    assert vault.labels(note_id).get("sliced") == ""


def test_minting_without_a_note_id_is_a_refusal(vault):
    with pytest.raises(ValueError, match="note_id"):
        call("slice_expose")


# -- resolving ------------------------------------------------------------


def test_resolving_a_bound_token(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven", write=True)
    out = call("slice_resolve", token=minted["token"])
    assert out == {"found": True, "note_id": note_id, "write": True,
                   "user": "steven"}


def test_resolving_an_open_token(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id)
    out = call("slice_resolve", token=minted["token"])
    assert out == {"found": True, "note_id": note_id, "write": False,
                   "user": None}


def test_resolving_an_unknown_token(vault):
    out = call("slice_resolve", token="not-a-real-token")
    assert out == {"found": False, "note_id": None, "write": False,
                   "user": None}


def test_resolving_a_revoked_token(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven")
    call("slice_revoke", token=minted["token"])
    out = call("slice_resolve", token=minted["token"])
    assert out == {"found": False, "note_id": None, "write": False,
                   "user": None}


def test_resolving_an_expired_token(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven",
                  expires_in_hours=-1)
    out = call("slice_resolve", token=minted["token"])
    assert out == {"found": False, "note_id": None, "write": False,
                   "user": None}


def test_an_unexpired_token_still_resolves(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven",
                  expires_in_hours=24)
    out = call("slice_resolve", token=minted["token"])
    assert out["found"] is True


# -- revoking -------------------------------------------------------------


def test_revoking_clears_the_sliced_label_when_no_slice_remains(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven")
    out = call("slice_revoke", token=minted["token"])
    assert out == {"token": minted["token"], "note_id": note_id,
                   "revoked": True, "sliced_cleared": True}
    assert "sliced" not in vault.labels(note_id)


def test_a_second_live_slice_keeps_the_label_up(vault):
    """A note may be exposed to two people at once; the label is a fact about
    the note, not about one token."""
    note_id = _note(vault)
    first = call("slice_expose", note_id=note_id, user="steven")
    call("slice_expose", note_id=note_id, user="tony")
    out = call("slice_revoke", token=first["token"])
    assert out["sliced_cleared"] is False
    assert vault.labels(note_id).get("sliced") == ""


def test_revoking_an_unknown_token_is_a_refusal(vault):
    with pytest.raises(ValueError, match="no slice"):
        call("slice_revoke", token="not-a-real-token")


def test_revoking_twice_is_idempotent(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven")
    call("slice_revoke", token=minted["token"])
    out = call("slice_revoke", token=minted["token"])
    assert out["revoked"] is True


# -- listing ----------------------------------------------------------------


def test_listing_reports_bound_and_open_tokens(vault):
    note_id = _note(vault)
    call("slice_expose", note_id=note_id, user="steven")
    call("slice_expose", note_id=note_id)
    out = call("slice_list")
    users = sorted((s["user"] or "") for s in out["slices"])
    assert users == ["", "steven"]


def test_listing_can_be_restricted_to_one_note(vault):
    a = _note(vault)
    b = _note(vault)
    call("slice_expose", note_id=a, user="steven")
    call("slice_expose", note_id=b, user="tony")
    out = call("slice_list", note_id=a)
    assert [s["note_id"] for s in out["slices"]] == [a]


def test_a_revoked_slice_still_appears_in_the_list(vault):
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven")
    call("slice_revoke", token=minted["token"])
    out = call("slice_list", note_id=note_id)
    assert out["slices"][0]["active"] is False
    assert out["slices"][0]["revoked_at"] is not None


def test_a_node_serving_the_tarball_refuses_slices(vault, monkeypatch):
    """The mask lives in the fork, so upstream's build would answer everything.

    Refused at both ends: a database reaching such a node by sync carries tokens
    minted where the fork was running.
    """
    note_id = _note(vault)
    minted = call("slice_expose", note_id=note_id, user="steven")

    monkeypatch.setattr(hub_adapter.instances, "entry_point",
                        lambda: (object(), "tarball"))

    with pytest.raises(PermissionError, match="needs the Trilium fork"):
        call("slice_expose", note_id=note_id)
    with pytest.raises(PermissionError, match="needs the Trilium fork"):
        call("slice_resolve", token=minted["token"])
