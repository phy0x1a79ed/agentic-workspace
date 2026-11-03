"""The scope brief: one in force per scope, every write kept, a 400-character cap (scopes.md R143)."""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.journal]


def _run(verb, args, as_=None):
    from awm.journal.operations.briefs import BRIEF_HANDLERS
    return BRIEF_HANDLERS[verb](args, as_)


def test_a_scope_with_no_brief_reads_none(journal_workspace):
    assert _run("brief_read", {"project": "awm", "scope": "s"}) == {"brief": None}


def test_set_supersedes_and_read_returns_the_newest(journal_workspace):
    first = _run("brief_set", {"project": "awm", "scope": "s", "text": "For the scopes service."})
    second = _run("brief_set", {"project": "awm", "scope": "s", "text": "For the journal."})
    assert second["brief"]["supersedes"] == first["brief"]["id"]
    out = _run("brief_read", {"project": "awm", "scope": "s"})["brief"]
    assert out["text"] == "For the journal." and out["author"] == "agent:awm/s"
    assert _run("brief_read", {"project": "awm", "scope": "other"})["brief"] is None


def test_setting_the_same_text_writes_nothing(journal_workspace):
    a = _run("brief_set", {"project": "awm", "scope": "s", "text": "For x."})["brief"]
    b = _run("brief_set", {"project": "awm", "scope": "s", "text": "For x."})["brief"]
    assert a["id"] == b["id"]


def test_a_brief_over_400_characters_is_refused(journal_workspace):
    from awm.journal.briefs import BriefError
    with pytest.raises(BriefError, match="limit is 400"):
        _run("brief_set", {"project": "awm", "scope": "s", "text": "x" * 401})
    _run("brief_set", {"project": "awm", "scope": "s", "text": "x" * 400})


def test_edit_replaces_one_passage(journal_workspace):
    from awm.journal.briefs import BriefError
    _run("brief_set", {"project": "awm", "scope": "s", "text": "For the scopes service, in awm/services/scopes."})
    out = _run("brief_edit", {"project": "awm", "scope": "s", "old": "scopes service",
                              "new": "journal service"})["brief"]
    assert out["text"] == "For the journal service, in awm/services/scopes."
    with pytest.raises(BriefError, match="exactly once"):
        _run("brief_edit", {"project": "awm", "scope": "s", "old": "nowhere", "new": "x"})
    with pytest.raises(BriefError, match="limit"):
        _run("brief_edit", {"project": "awm", "scope": "s", "old": "For", "new": "y" * 400})


def test_edit_needs_a_brief(journal_workspace):
    from awm.journal.briefs import BriefError
    with pytest.raises(BriefError, match="brief_set"):
        _run("brief_edit", {"project": "awm", "scope": "s", "old": "a", "new": "b"})


def test_a_foreign_peer_reads_no_brief(journal_workspace):
    with pytest.raises(ValueError, match="no scope"):
        _run("brief_read", {"project": "awm", "scope": "s"}, as_="peer:stranger")


def test_the_write_verbs_carry_the_r143_text():
    from awm.journal.operations.briefs import BRIEF_MANIFEST_FUNCTIONS
    by = {f["name"]: f for f in BRIEF_MANIFEST_FUNCTIONS}
    for verb in ("brief_set", "brief_edit"):
        assert by[verb]["description"].startswith(
            "The scope brief tells an agent what this scope is for within its project.")
        assert "A brief over 400 characters is refused." in by[verb]["description"]
