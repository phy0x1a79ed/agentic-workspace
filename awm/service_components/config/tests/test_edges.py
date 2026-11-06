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


def test_a_singleton_admits_its_host_and_its_callers():
    assert edges.may_call("spare", "2fa") and edges.may_call("Home.lan", "2fa")
    assert not edges.may_call("work", "2fa") and not edges.may_call(None, "2fa")
    assert edges.may_call("work", "channel") and edges.may_call("work", "journal")


def test_a_planned_caller_is_left_out(tmp_path, monkeypatch):
    planned = tmp_path / "edges.yml"
    planned.write_text("nodes: {a: {role: standby}, b: {role: admin, planned: true}, c: {role: work}}\n"
                       "singleton_doors: {a: {port: 2201, network: lan}}\n"
                       "singletons: [{tool: t, host: a, callers: [b, c]}]\n")
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(planned))
    assert edges.may_call("c", "t") and not edges.may_call("b", "t")


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


@pytest.mark.parametrize("singleton, error", [
    ("{tool: t, host: a}", "no callers list"),
    ("{tool: t, host: a, callers: [b]}", "caller b is no node"),
])
def test_rejects_a_bad_callers_list(tmp_path, monkeypatch, singleton, error):
    bad = tmp_path / "edges.yml"
    bad.write_text("nodes: {a: {role: standby}}\nsingleton_doors: {a: {port: 2201, network: lan}}\n"
                   f"singletons: [{singleton}]\n")
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(bad))
    with pytest.raises(ValueError, match=error):
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


def test_caller_peer_names_the_node_of_a_peer_stamp():
    from awm.config import caller_peer
    assert caller_peer("peer:mira") == "mira"
    for value in (None, "", "peer", "peer:", "tony", "user:mira", "mira"):
        assert caller_peer(value) is None


def test_operator_dm_falls_back_to_tonys_dm(monkeypatch):
    import awm.config as config
    for var in ("AWM_OPERATOR_DM_ACCOUNT", "AWM_OPERATOR_DM_CHANNEL"):
        monkeypatch.delenv(var, raising=False)
    assert config.operator_dm() == ("discord-bot", "1522674357762261112")
    monkeypatch.setenv("AWM_OPERATOR_DM_CHANNEL", " dm:1 ")
    monkeypatch.setenv("AWM_OPERATOR_DM_ACCOUNT", "other-bot")
    assert config.operator_dm() == ("other-bot", "dm:1")
