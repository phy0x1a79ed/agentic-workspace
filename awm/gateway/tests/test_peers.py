"""Peer book writes: full record, old-entry loading, key pinning, fingerprint."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

import awm.config as cfg
from awm.gateway import gateway_ops, peers

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

KEY_BYTES = bytes(range(32))
KEY = base64.b64encode(KEY_BYTES).decode()
KEY_FP = "SHA256:" + base64.b64encode(hashlib.sha256(KEY_BYTES).digest()).decode().rstrip("=")
RETIRED = ("relation", "swarm", "principal", "role", "grants")


@pytest.fixture(autouse=True)
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path)
    return tmp_path / "state" / "peers.json"


def _seed(book, data):
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text(json.dumps(data))


def test_add_writes_the_full_record(book):
    rec = peers.add("mira", "mira:12100")
    assert rec["edge_url"] == "https://mira:12100"
    assert rec["ssh_alias"] == "mira"
    assert rec["public_key"] is None and rec["key_fingerprint"] is None
    assert not set(RETIRED) & set(rec)
    assert json.loads(book.read_text())["mira"]["edge_url"] == "https://mira:12100"


def test_old_entry_loads_and_survives_other_writes(book):
    _seed(book, {"mira": {"name": "mira", "edge_url": "https://mira:1",
                          "ssh_alias": "m", "added_at": 5.0}})
    rec = peers.resolve("mira")
    assert rec["public_key"] is None and rec["key_fingerprint"] is None
    assert [r["name"] for r in peers.list_all()] == ["mira"]
    # a write to another peer leaves the old entry's raw shape untouched
    peers.add("capella", "capella:1")
    assert json.loads(book.read_text())["mira"] == {
        "name": "mira", "edge_url": "https://mira:1", "ssh_alias": "m", "added_at": 5.0}


def test_a_write_drops_the_retired_trust_fields(book):
    _seed(book, {"envoy": {"edge_url": "https://envoy:1", "relation": "old",
                           "swarm": "blue", "principal": "ann", "role": "station",
                           "grants": ["kb"]}})
    assert not set(RETIRED) & set(peers.resolve("envoy"))
    peers.update("envoy", public_key=KEY)
    assert not set(RETIRED) & set(json.loads(book.read_text())["envoy"])


@pytest.mark.parametrize("shape", ["[]", "null", "{bad", '{"x": 3}'])
def test_odd_book_shapes_are_empty(book, shape):
    book.parent.mkdir(parents=True)
    book.write_text(shape)
    assert peers.list_all() == []
    assert peers.resolve("x") is None


def test_add_with_a_key(book):
    rec = peers.add("envoy", "envoy:1", public_key=KEY)
    assert rec["public_key"] == KEY and rec["key_fingerprint"] == KEY_FP


def test_readd_keeps_the_key_and_added_at(book):
    first = peers.add("envoy", "envoy:1", public_key=KEY)
    again = peers.add("envoy", "envoy:2")
    assert again["edge_url"] == "https://envoy:2"
    assert again["key_fingerprint"] == KEY_FP
    assert again["added_at"] == first["added_at"]


@pytest.mark.parametrize("shape", ["{bad", "[]", "null", '"x"'])
def test_writers_refuse_a_corrupt_book(book, shape):
    book.parent.mkdir(parents=True)
    book.write_text(shape)
    for call in (
        lambda: peers.add("mira", "mira:1"),
        lambda: peers.update("mira", public_key=KEY),
        lambda: peers.remove("mira"),
    ):
        with pytest.raises(ValueError, match="peer book"):
            call()
    assert book.read_text() == shape
    assert peers.list_all() == []  # readers still treat it as empty


def test_stored_name_that_differs_from_key_is_ignored(book):
    _seed(book, {"mira": {"name": "other", "edge_url": "https://mira:1"}})
    assert peers.resolve("mira")["name"] == "mira"
    assert [r["name"] for r in peers.list_all()] == ["mira"]
    assert peers.update("mira", public_key=KEY)["name"] == "mira"
    assert json.loads(book.read_text())["mira"]["name"] == "mira"


def test_concurrent_writers_all_land_and_leave_no_temp_files(book):
    import threading
    errors = []

    def worker(i):
        try:
            peers.add(f"p{i}", f"p{i}:1")
            peers.update(f"p{i}", public_key=KEY)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    stored = json.loads(book.read_text())
    assert sorted(stored) == sorted(f"p{i}" for i in range(12))
    assert all(v["key_fingerprint"] == KEY_FP for v in stored.values())
    assert [f.name for f in book.parent.iterdir() if f.name.endswith(".tmp")] == []


def test_add_validates():
    with pytest.raises(ValueError):
        peers.add("x", "x:1", public_key="not base64!")
    for bad in ("", "a@b", "a/b"):
        with pytest.raises(ValueError):
            peers.add(bad, "x:1")


def test_set_pins_and_clears_the_key(book):
    peers.add("mira", "mira:1")
    rec = peers.update("mira", public_key=KEY)
    assert rec["public_key"] == KEY and rec["key_fingerprint"] == KEY_FP
    assert peers.resolve("mira")["key_fingerprint"] == KEY_FP
    cleared = peers.update("mira", public_key="")
    assert cleared["public_key"] is None and cleared["key_fingerprint"] is None


def test_set_requires_a_key_and_a_known_peer():
    peers.add("mira", "mira:1")
    with pytest.raises(ValueError, match="nothing to set"):
        peers.update("mira")
    with pytest.raises(FileNotFoundError):
        peers.update("ghost", public_key=KEY)


def test_list_and_resolve_show_every_field():
    peers.add("mira", "mira:1")
    fields = {"name", "edge_url", "ssh_alias", "public_key", "key_fingerprint"}
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
    req = gateway_ops.PeerJoinRequest(name="envoy", edge_url="envoy:1")
    assert gateway_ops._op_peer_join(req)["peer"]["edge_url"] == "https://envoy:1"
    out = gateway_ops._op_peer_set(gateway_ops.PeerSetRequest(name="envoy", public_key=KEY))
    assert out["peer"]["key_fingerprint"] == KEY_FP
    assert gateway_ops._op_peer_list()["peers"][0]["name"] == "envoy"


def test_the_trust_verbs_are_gone():
    names = {o.name for o in gateway_ops.GATEWAY_OPERATIONS}
    assert not {"operations_peer_grant", "operations_peer_revoke"} & names
    join = next(o for o in gateway_ops.GATEWAY_OPERATIONS if o.name == "operations_peer_join")
    assert not set(RETIRED) & {p.name for p in join.params}


def test_peer_op_effects():
    effects = {o.name: o.effect for o in gateway_ops.GATEWAY_OPERATIONS
               if o.mcp_tool == "operations"}
    for name in ("operations_peer_list", "operations_peer_resolve"):
        assert effects[name] == "read"
    for name in ("operations_peer_add", "operations_peer_join", "operations_peer_set",
                 "operations_peer_forget"):
        assert effects[name] == "write"
