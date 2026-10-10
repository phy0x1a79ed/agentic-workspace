"""Peer book writes: full record, old-entry loading, set/grant/revoke, fingerprint."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

import awm.config as cfg
from awm.gateway import gateway_ops, peers

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

KEY_BYTES = bytes(range(32))
KEY = base64.b64encode(KEY_BYTES).decode()
KEY_FP = "SHA256:" + base64.b64encode(hashlib.sha256(KEY_BYTES).digest()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path)
    for var in ("AWM_NODE_ROLE", "AWM_SWARM"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "state" / "peers.json"


def _seed(book, data):
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text(json.dumps(data))


def test_add_writes_full_record_with_domestic_defaults(book):
    rec = peers.add("mira", "mira:12100")
    assert rec["edge_url"] == "https://mira:12100"
    assert rec["ssh_alias"] == "mira"
    assert (rec["relation"], rec["swarm"], rec["role"]) == ("domestic", "tony", "fleet")
    assert rec["principal"] is None and rec["public_key"] is None
    assert rec["key_fingerprint"] is None and rec["grants"] == []
    assert json.loads(book.read_text())["mira"]["relation"] == "domestic"


def test_old_entry_loads_as_domestic_and_survives_other_writes(book):
    _seed(book, {"mira": {"name": "mira", "edge_url": "https://mira:1",
                          "ssh_alias": "m", "added_at": 5.0}})
    rec = peers.resolve("mira")
    assert (rec["relation"], rec["swarm"], rec["role"]) == ("domestic", "tony", "fleet")
    assert rec["public_key"] is None and rec["key_fingerprint"] is None
    assert rec["grants"] == []
    assert [r["name"] for r in peers.list_all()] == ["mira"]
    # a write to another peer leaves the old entry's raw shape untouched
    peers.add("capella", "capella:1")
    assert json.loads(book.read_text())["mira"] == {
        "name": "mira", "edge_url": "https://mira:1", "ssh_alias": "m", "added_at": 5.0}


@pytest.mark.parametrize("shape", ["[]", "null", "{bad", '{"x": 3}'])
def test_odd_book_shapes_are_empty(book, shape):
    book.parent.mkdir(parents=True)
    book.write_text(shape)
    assert peers.list_all() == []
    assert peers.resolve("x") is None


def test_add_with_trust_fields_and_key(book):
    rec = peers.add("envoy", "envoy:1", relation="foreign", swarm="blue",
                    principal="ann", role="station", public_key=KEY)
    assert (rec["relation"], rec["swarm"], rec["principal"], rec["role"]) == (
        "foreign", "blue", "ann", "station")
    assert rec["key_fingerprint"] == KEY_FP


def test_readd_keeps_trust_fields_and_added_at(book):
    first = peers.add("envoy", "envoy:1", relation="foreign", swarm="blue")
    again = peers.add("envoy", "envoy:2")
    assert again["edge_url"] == "https://envoy:2"
    assert again["relation"] == "foreign" and again["swarm"] == "blue"
    assert again["added_at"] == first["added_at"]


def test_foreign_add_needs_swarm():
    with pytest.raises(ValueError, match="swarm"):
        peers.add("envoy", "envoy:1", relation="foreign")


@pytest.mark.parametrize("kw", [{"relation": "friendly"}, {"role": "admiral"},
                                {"swarm": "Blue Team"}, {"principal": "a b"},
                                {"public_key": "not base64!"}])
def test_add_validates(kw):
    with pytest.raises(ValueError):
        peers.add("x", "x:1", **kw)


def test_set_updates_fields_and_fingerprint(book):
    peers.add("mira", "mira:1")
    rec = peers.update("mira", relation="foreign", swarm="red", principal="bo",
                       role="station", public_key=KEY)
    assert (rec["relation"], rec["swarm"], rec["principal"], rec["role"]) == (
        "foreign", "red", "bo", "station")
    assert rec["public_key"] == KEY and rec["key_fingerprint"] == KEY_FP
    assert peers.resolve("mira")["key_fingerprint"] == KEY_FP
    cleared = peers.update("mira", public_key="")
    assert cleared["public_key"] is None and cleared["key_fingerprint"] is None


def test_set_requires_a_field_and_a_known_peer():
    peers.add("mira", "mira:1")
    with pytest.raises(ValueError, match="nothing to set"):
        peers.update("mira")
    with pytest.raises(FileNotFoundError):
        peers.update("ghost", role="fleet")


def test_set_converts_an_old_entry_in_place(book):
    _seed(book, {"mira": {"edge_url": "https://mira:1"}})
    peers.update("mira", principal="tony")
    stored = json.loads(book.read_text())["mira"]
    assert stored["principal"] == "tony" and stored["relation"] == "domestic"


def test_grant_and_revoke(book):
    peers.add("envoy", "envoy:1", relation="foreign", swarm="blue")
    rec, warning = peers.grant("envoy", "journals")
    assert rec["grants"] == ["journals"] and warning is None
    peers.grant("envoy", "kb")
    rec, _ = peers.grant("envoy", "journals")
    assert rec["grants"] == ["journals", "kb"]
    assert peers.revoke("envoy", "journals")["grants"] == ["kb"]
    assert peers.revoke("envoy", "journals")["grants"] == ["kb"]
    assert json.loads(book.read_text())["envoy"]["grants"] == ["kb"]


def test_grant_to_domestic_warns_and_still_stores():
    peers.add("mira", "mira:1")
    rec, warning = peers.grant("mira", "kb")
    assert rec["grants"] == ["kb"]
    assert "domestic" in warning


def test_grant_validates_slug_and_peer():
    peers.add("mira", "mira:1")
    for bad in ("Journals", "a b", ""):
        with pytest.raises(ValueError):
            peers.grant("mira", bad)
    with pytest.raises(FileNotFoundError):
        peers.grant("ghost", "kb")
    with pytest.raises(FileNotFoundError):
        peers.revoke("ghost", "kb")


def test_list_and_resolve_show_every_field():
    peers.add("mira", "mira:1")
    fields = {"name", "edge_url", "ssh_alias", "relation", "swarm", "principal",
              "role", "public_key", "key_fingerprint", "grants"}
    assert fields <= set(peers.list_all()[0])
    assert fields <= set(peers.resolve("mira"))
    assert fields <= set(gateway_ops._op_peer_resolve("mira")["peer"])


def test_remove():
    peers.add("mira", "mira:1")
    assert peers.remove("mira")["name"] == "mira"
    assert peers.remove("mira") is None
    assert peers.list_all() == []


# --- verb layer -------------------------------------------------------------


def test_verbs_roundtrip():
    req = gateway_ops.PeerJoinRequest(name="envoy", edge_url="envoy:1",
                                      relation="foreign", swarm="blue")
    assert gateway_ops._op_peer_join(req)["peer"]["relation"] == "foreign"
    out = gateway_ops._op_peer_set(gateway_ops.PeerSetRequest(name="envoy", public_key=KEY))
    assert out["peer"]["key_fingerprint"] == KEY_FP
    grant = gateway_ops._op_peer_grant(gateway_ops.PeerGrantRequest(name="envoy", category="kb"))
    assert grant["peer"]["grants"] == ["kb"] and "warning" not in grant
    revoked = gateway_ops._op_peer_revoke(gateway_ops.PeerGrantRequest(name="envoy", category="kb"))
    assert revoked["peer"]["grants"] == []
    assert gateway_ops._op_peer_list()["peers"][0]["name"] == "envoy"


def test_grant_verb_warns_for_domestic():
    peers.add("mira", "mira:1")
    out = gateway_ops._op_peer_grant(gateway_ops.PeerGrantRequest(name="mira", category="kb"))
    assert "warning" in out


def test_peer_op_effects():
    effects = {o.name: o.effect for o in gateway_ops.GATEWAY_OPERATIONS
               if o.cli_group == "peer"}
    for name in ("peer_list", "peer_resolve"):
        assert effects[name] == "read"
    for name in ("peer_add", "peer_join", "peer_set", "peer_grant", "peer_revoke",
                 "peer_forget"):
        assert effects[name] == "write"


# --- startup warning --------------------------------------------------------


def _fake_discovery(monkeypatch, services):
    from awm.gateway.hub import discovery
    monkeypatch.setattr(discovery, "discover_services", lambda: [
        SimpleNamespace(name=n, enabled=e) for n, e in services])


def test_station_with_fleet_service_warns(monkeypatch, caplog):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    _fake_discovery(monkeypatch, [("cx", True), ("agents", False), ("kb", True)])
    with caplog.at_level(logging.WARNING, logger="awm.gateway.peers"):
        assert peers.warn_station_fleet_services() == ["cx"]
    assert "cx" in caplog.text


def test_fleet_node_or_clean_station_is_quiet(monkeypatch, caplog):
    _fake_discovery(monkeypatch, [("cx", True)])
    assert peers.warn_station_fleet_services() == []
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    _fake_discovery(monkeypatch, [("cx", False), ("kb", True)])
    assert peers.warn_station_fleet_services() == []
    assert not caplog.records
