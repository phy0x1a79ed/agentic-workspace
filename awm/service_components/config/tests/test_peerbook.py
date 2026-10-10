"""Peer book reader, node role/swarm env, and verb effect/category helpers."""

from __future__ import annotations

import json

import pytest

import awm.config as config
from awm.config import peerbook

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("AWM_NODE_ROLE", "AWM_SWARM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "AWM_DIR", tmp_path)


def _write_book(tmp_path, book):
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book))


def test_reexports():
    for name in ("node_role", "node_swarm", "peer_record", "peer_relation",
                 "caller_peer", "RELATIONS", "ROLES", "GRANT_CATEGORIES",
                 "EFFECTS", "verb_effect", "verb_category"):
        assert hasattr(config, name)
    assert config.RELATIONS == ("domestic", "foreign")
    assert config.ROLES == ("fleet", "station")
    assert config.GRANT_CATEGORIES == ("journals", "kb")


def test_node_role_default_and_values(monkeypatch):
    assert config.node_role() == "fleet"
    monkeypatch.setenv("AWM_NODE_ROLE", " station ")
    assert config.node_role() == "station"
    monkeypatch.setenv("AWM_NODE_ROLE", "")
    assert config.node_role() == "fleet"


def test_node_role_rejects_other_values(monkeypatch):
    monkeypatch.setenv("AWM_NODE_ROLE", "admiral")
    with pytest.raises(ValueError):
        config.node_role()


def test_node_swarm_default_and_strip(monkeypatch):
    assert config.node_swarm() == "tony"
    monkeypatch.setenv("AWM_SWARM", "  blue ")
    assert config.node_swarm() == "blue"
    monkeypatch.setenv("AWM_SWARM", "   ")
    assert config.node_swarm() == "tony"


def test_missing_or_corrupt_book_is_empty(tmp_path):
    assert config.peer_record("mira") is None
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert config.peer_record("mira") is None
    path.write_text("[]")
    assert config.peer_record("mira") is None


def test_old_entry_normalised_with_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_SWARM", "blue")
    _write_book(tmp_path, {"mira": {
        "name": "mira", "edge_url": "https://mira:12100", "ssh_alias": "mira",
        "added_at": 1.0, "updated_at": 2.0}})
    rec = config.peer_record("mira")
    assert rec == {
        "name": "mira", "edge_url": "https://mira:12100", "ssh_alias": "mira",
        "relation": "domestic", "swarm": "blue", "principal": None,
        "role": "fleet", "public_key": None, "key_fingerprint": None,
        "grants": [],
        "added_at": 1.0, "updated_at": 2.0,
    }
    assert config.peer_relation("mira") == "domestic"


def test_full_entry_kept(tmp_path):
    _write_book(tmp_path, {"envoy": {
        "name": "envoy", "edge_url": "https://envoy:1", "ssh_alias": "e",
        "relation": "foreign", "swarm": "other", "principal": "ann",
        "role": "station", "public_key": "AAAA", "key_fingerprint": "SHA256:x",
        "grants": ["journals", "future-category"]}})
    rec = config.peer_record("envoy")
    assert rec["relation"] == "foreign"
    assert rec["principal"] == "ann"
    assert rec["role"] == "station"
    assert rec["public_key"] == "AAAA"
    assert rec["grants"] == ["journals", "future-category"]
    assert config.peer_relation("envoy") == "foreign"


def test_unknown_peer(tmp_path):
    _write_book(tmp_path, {})
    assert config.peer_record("nobody") is None
    assert config.peer_relation("nobody") is None


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


def test_verb_effect_and_category():
    assert config.EFFECTS == ("read", "queue", "write", "secret")
    assert config.verb_effect({"name": "x"}) == "write"
    assert config.verb_effect({"name": "x", "effect": None}) == "write"
    assert config.verb_effect({"name": "x", "effect": "read"}) == "read"
    with pytest.raises(ValueError):
        config.verb_effect({"name": "x", "effect": "delete"})
    assert config.verb_category({"name": "x"}) is None
    assert config.verb_category({"name": "x", "category": "kb"}) == "kb"


def test_list_records_normalises_and_sorts(tmp_path):
    _write_book(tmp_path, {"b": {"edge_url": "https://b"}, "a": {"relation": "foreign"},
                           "junk": "not-a-dict"})
    recs = config.list_records()
    assert [r["name"] for r in recs] == ["a", "b"]
    assert recs[0]["relation"] == "foreign" and recs[1]["relation"] == "domestic"
    assert recs[1]["grants"] == []


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
