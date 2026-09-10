"""Reaching the library, and what the library is called while we do.

Almost all of this module is one HTTPS request, so what is worth testing is the
naming: the mirror writes a library's name onto 823 notes, and a name that
changes is 823 notes that stop being found.
"""

from __future__ import annotations

import pytest

from awm.zotero import source

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_the_personal_library_keeps_the_name_the_vault_already_carries(monkeypatch):
    """The migration this exists for.

    The desktop's copy of the interface numbers the signed-in user 0, and every
    note the mirror has written carries `users/0/<key>`. The published interface
    numbers the account for real. Letting that number reach the bundle would
    change the identity of every paper at once: the pass would find no note for
    any reference, build a second copy of the whole library, and then delete the
    first for being absent.
    """
    monkeypatch.setattr(source, "USER", "5331043")
    assert source.personal() == "users/0"
    assert [lib["id"] for lib in [{"id": source.personal()}]] == ["users/0"]


def test_the_number_appears_only_in_an_address(monkeypatch):
    monkeypatch.setattr(source, "USER", "5331043")
    assert source.path_of("users/0") == "users/5331043"
    # A group is numbered the same by both, so it passes straight through.
    assert source.path_of("groups/5284390") == "groups/5284390"


def test_the_account_number_is_discovered_rather_than_configured(monkeypatch):
    """A key already knows whose it is. A second setting is a second thing to
    get wrong, and getting it wrong reads somebody else's library or nothing."""
    monkeypatch.setattr(source, "USER", "")
    monkeypatch.setattr(source, "whoami",
                        lambda: {"user_id": "42", "username": "someone",
                                 "writes": False, "groups_read": True})
    assert source.path_of("users/0") == "users/42"


def test_a_missing_key_is_refused_with_what_to_do(monkeypatch):
    monkeypatch.setattr(source, "KEY", "")
    with pytest.raises(source.ZoteroError, match="read-only"):
        source._get("/keys/current")


def test_the_service_asking_for_patience_is_remembered(monkeypatch):
    """A public interface asks by header rather than by refusing, so honouring
    it is on us. Ignoring it is how a well-behaved client becomes a blocked one."""
    monkeypatch.setattr(source, "_not_before", 0.0)
    source._note_backoff({"backoff": "12"})
    assert source._not_before > 0
    monkeypatch.setattr(source, "_not_before", 0.0)
    source._note_backoff({"retry-after": "30"})
    assert source._not_before > 0
    # Nonsense must not raise; a request answered oddly still answered.
    source._note_backoff({"backoff": "soon"})
