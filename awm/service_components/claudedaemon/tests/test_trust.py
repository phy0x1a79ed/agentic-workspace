"""The trusted-directory check, against a fake `~/.claude.json`."""
from __future__ import annotations

import json

from awm.claudedaemon import trust


def trust_file(tmp_path, projects):
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"projects": projects}))
    return f


def test_trust_is_inherited_from_an_ancestor(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": True}})
    assert trust.trusted(tmp_path, trust_file=f)
    assert trust.trusted(tmp_path / "a" / "b", trust_file=f)


def test_a_directory_with_no_trusted_ancestor_is_untrusted(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path / "other"): {"hasTrustDialogAccepted": True}})
    assert not trust.trusted(tmp_path / "here", trust_file=f)


def test_a_declined_entry_is_not_trust(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": False}})
    assert not trust.trusted(tmp_path, trust_file=f)


def test_an_unreadable_or_reshaped_file_reads_as_untrusted(tmp_path):
    assert not trust.trusted(tmp_path, trust_file=tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert not trust.trusted(tmp_path, trust_file=bad)
    bad.write_text(json.dumps({"projects": ["not", "a", "dict"]}))
    assert not trust.trusted(tmp_path, trust_file=bad)
    bad.write_text("[]")
    assert not trust.trusted(tmp_path, trust_file=bad)
