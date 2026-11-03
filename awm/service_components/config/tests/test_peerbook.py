"""Peer book reader, operator DM setting, and the verb effect helper."""

from __future__ import annotations

import json

import pytest

import awm.config as config
from awm.config import peerbook

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("AWM_OPERATOR_DM_ACCOUNT",
                "AWM_OPERATOR_DM_CHANNEL", "AWM_REPORTING_DM_CHANNEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "AWM_DIR", tmp_path)


def _write_book(tmp_path, book):
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book))


def test_reexports():
    for name in ("peer_record", "caller_peer", "list_records", "load_book",
                 "operator_dm", "EFFECTS", "verb_effect"):
        assert hasattr(config, name)
    for gone in ("ROLES", "ARCHIVE_CATEGORY", "verb_category"):
        assert not hasattr(config, gone), gone


def test_operator_dm_falls_back_to_tonys_dm():
    assert config.operator_dm() == ("discord-bot", "dm:188743359983124480")


def test_operator_dm_setting_wins(monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_DM_CHANNEL", "dm:2")
    assert config.operator_dm() == ("discord-bot", "dm:2")
    monkeypatch.setenv("AWM_OPERATOR_DM_CHANNEL", " dm:1 ")
    monkeypatch.setenv("AWM_OPERATOR_DM_ACCOUNT", "other-bot")
    assert config.operator_dm() == ("other-bot", "dm:1")


def test_missing_or_corrupt_book_is_empty(tmp_path):
    assert config.peer_record("mira") is None
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert config.peer_record("mira") is None
    path.write_text("[]")
    assert config.peer_record("mira") is None


def test_old_entry_normalised_with_defaults(tmp_path):
    _write_book(tmp_path, {"mira": {
        "name": "mira", "edge_url": "https://mira:12100", "ssh_alias": "mira",
        "added_at": 1.0, "updated_at": 2.0}})
    rec = config.peer_record("mira")
    assert rec == {
        "name": "mira", "edge_url": "https://mira:12100", "ssh_alias": "mira",
        "public_key": None, "key_fingerprint": None,
        "added_at": 1.0, "updated_at": 2.0,
    }


def test_retired_trust_fields_are_dropped(tmp_path):
    _write_book(tmp_path, {"envoy": {
        "name": "envoy", "edge_url": "https://envoy:1", "ssh_alias": "e",
        "relation": "old", "swarm": "other", "principal": "ann",
        "role": "station", "public_key": "AAAA", "key_fingerprint": "SHA256:x",
        "grants": ["journals"]}})
    rec = config.peer_record("envoy")
    assert rec["public_key"] == "AAAA" and rec["ssh_alias"] == "e"
    for gone in ("relation", "swarm", "principal", "role", "grants"):
        assert gone not in rec, gone


def test_unknown_peer(tmp_path):
    _write_book(tmp_path, {})
    assert config.peer_record("nobody") is None


def test_path_resolves_at_call_time(tmp_path, monkeypatch):
    other = tmp_path / "other"
    _write_book(other, {"a": {"edge_url": "https://a"}})
    assert config.peer_record("a") is None
    monkeypatch.setattr(config, "AWM_DIR", other)
    assert config.peer_record("a")["name"] == "a"
    assert peerbook.peers_file() == other / "state" / "peers.json"


def test_caller_peer_returns_node_name_without_book_lookup(tmp_path):
    _write_book(tmp_path, {})
    assert config.caller_peer("peer:mira") == "mira"
    assert config.caller_peer("peer:ghost") == "ghost"
    for value in (None, "", "peer", "peer:", "tony", "user:mira", "mira"):
        assert config.caller_peer(value) is None


def test_verb_effect():
    assert config.EFFECTS == ("read", "queue", "write", "secret")
    assert config.verb_effect({"name": "x"}) == "write"
    assert config.verb_effect({"name": "x", "effect": None}) == "write"
    assert config.verb_effect({"name": "x", "effect": "read"}) == "read"
    with pytest.raises(ValueError):
        config.verb_effect({"name": "x", "effect": "delete"})


def test_list_records_normalises_and_sorts(tmp_path):
    _write_book(tmp_path, {"b": {"edge_url": "https://b"}, "a": {"relation": "old"},
                           "junk": "not-a-dict"})
    recs = config.list_records()
    assert [r["name"] for r in recs] == ["a", "b"]
    assert "relation" not in recs[0] and recs[1]["ssh_alias"] == "b"


def test_fingerprint_of_raw_key_bytes():
    import base64
    import hashlib
    raw = bytes(range(32))
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    assert peerbook._fingerprint(base64.b64encode(raw).decode()) == expected
    assert not expected.endswith("=")
    for bad in ("", "  ", "***"):
        with pytest.raises(ValueError):
            peerbook._fingerprint(bad)


def test_normalise_forces_name_to_book_key(tmp_path):
    _write_book(tmp_path, {"mira": {"name": "someone-else", "edge_url": "https://m"}})
    assert config.peer_record("mira")["name"] == "mira"
    assert [r["name"] for r in config.list_records()] == ["mira"]


def test_load_book_strict_raises_on_damage_only(tmp_path):
    assert peerbook.load_book(strict=True) == {}  # missing file is an empty book
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True)
    for bad in ("{nope", "[]"):
        path.write_text(bad)
        assert peerbook.load_book() == {}
        with pytest.raises(ValueError):
            peerbook.load_book(strict=True)
