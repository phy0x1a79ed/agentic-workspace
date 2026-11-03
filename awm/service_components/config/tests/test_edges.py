"""The edge list reader answers from a node's edges.yml (fleet.md R87, R96, R98)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.config import edges

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

FIXTURE = Path(__file__).parent / "fixtures" / "edges.yml"


@pytest.fixture(autouse=True)
def fixture_file(monkeypatch):
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(FIXTURE))


def test_edges_are_directional():
    assert edges.reaches("home", "work")
    assert not edges.reaches("work", "home")
    assert edges.reaches("relay", "relay")
    assert edges.reachable_from("spare") == ["home"]
    assert edges.reached_by("spare") == ["home", "laptop"]


def test_stations_reach_nothing():
    for name, node in edges.nodes().items():
        if node["role"] == "station":
            assert edges.reachable_from(name) == [], name


def test_one_network_per_edge():
    assert edges.network("home", "spare") == "lan"
    assert edges.network("laptop", "home") == "tailscale"
    assert edges.network("work", "backup") == "zerotier"
    assert edges.network("spare", "work") is None


def test_singletons_have_doors():
    assert edges.singleton_tools() == ["2fa", "channel"]
    assert edges.singleton_door("spare")["port"] == 2201
    assert edges.singleton_door("home") is None


def test_default_path_is_node_config(monkeypatch, tmp_path):
    import awm.config as config
    monkeypatch.delenv(edges.EDGES_FILE_ENV)
    monkeypatch.setattr(config, "AWM_DIR", tmp_path)
    assert edges.edges_file() == tmp_path / "edges.yml"


def test_rejects_unknown_network(tmp_path, monkeypatch):
    bad = tmp_path / "edges.yml"
    bad.write_text("nodes: {a: {role: homebase}, b: {role: station}}\nedges: {a: {b: pigeon}}\n")
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(bad))
    with pytest.raises(ValueError, match="pigeon"):
        edges.load()


def test_missing_file_is_empty_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(tmp_path / "none.yml"))
    assert edges.reachable_from("home") == []
    assert not edges.reaches("home", "spare")


def test_admits_a_peer_only_over_an_edge_to_this_node(monkeypatch):
    monkeypatch.setenv("AWM_NODE_NAME", "work")
    assert edges.here() == "work"
    assert edges.admits("peer:home")
    assert not edges.admits("peer:spare")
    assert not edges.admits("peer:")
    assert not edges.admits("peer:  ")
    assert not edges.admits(" PEER:Spare ")
    assert edges.admits(" Peer:HOME ")
    assert not edges.admits("peer")
    assert not edges.admits(" Peer ")
    for stamp in (None, "", "user:operator", "agent:awm/dev"):
        assert edges.admits(stamp), stamp


def test_role_of_a_listed_and_an_unlisted_node():
    assert edges.role("relay") == "station"
    assert edges.role("nowhere") is None


def test_names_compare_by_first_label(monkeypatch):
    monkeypatch.setenv("AWM_NODE_NAME", "Work.example.org")
    assert edges.here() == "work"
    assert edges.reaches("HOME.lan", "work")
    assert edges.admits("peer:home.example.org")
