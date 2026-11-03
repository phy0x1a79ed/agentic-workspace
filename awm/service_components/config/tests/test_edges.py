"""The fleet edge list matches fleet.md R87 row by row, and the reader answers from it."""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.config import edges

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

REPO_EDGES = Path(__file__).resolve().parents[4] / "fleet" / "edges.yml"

# fleet.md R87: each row is the node that connects out.
R87 = {
    "lyra":    {"capella", "mizar", "alcor", "altair", "deneb", "sockeye", "fir"},
    "capella": {"lyra", "mizar", "alcor", "altair", "deneb", "sockeye", "fir"},
    "mizar":   {"lyra", "capella", "alcor", "altair", "deneb", "sockeye", "fir"},
    "alcor":   {"mizar"},
    "altair":  {"sockeye", "fir", "macpro"},
    "deneb":   set(),
    "sockeye": set(),
    "fir":     set(),
    "macpro":  {"altair"},
}
# mira is in no R87 row. It keeps today's inbound path until the trial week ends.
RETIRING = {"mira"}


@pytest.fixture(autouse=True)
def repo_file(monkeypatch):
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(REPO_EDGES))


def test_table_matches_r87():
    for src, targets in R87.items():
        assert set(edges.reachable_from(src)) - RETIRING == targets, src


def test_stations_reach_nothing():
    for name, node in edges.nodes().items():
        if node["role"] == "station":
            assert edges.reachable_from(name) == [], name


def test_reach_queries():
    assert edges.reaches("mizar", "altair")
    assert not edges.reaches("altair", "mizar")
    assert not edges.reaches("altair", "deneb")
    assert edges.reaches("deneb", "deneb")
    assert edges.reached_by("alcor") == ["capella", "lyra", "mizar"]
    assert edges.network("mizar", "alcor") == "lan"
    assert edges.network("lyra", "mizar") == "tailscale"
    assert edges.network("altair", "macpro") == "zerotier"
    assert edges.network("alcor", "altair") is None


def test_singletons_have_doors():
    assert "2fa" in edges.singleton_tools()
    assert edges.singleton_door("alcor")["port"] == 2201
    assert edges.singleton_door("mizar") is None


def test_managed_set_is_r108():
    managed = {n for n, v in edges.nodes().items() if v.get("managed")}
    assert managed == {"mizar", "alcor", "mira", "capella", "lyra", "deneb", "shogun"}


def test_rejects_unknown_network(tmp_path, monkeypatch):
    bad = tmp_path / "edges.yml"
    bad.write_text("nodes: {a: {role: homebase}, b: {role: station}}\nedges: {a: {b: pigeon}}\n")
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(bad))
    with pytest.raises(ValueError, match="pigeon"):
        edges.load()


def test_missing_file_is_empty_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv(edges.EDGES_FILE_ENV, str(tmp_path / "none.yml"))
    assert edges.reachable_from("mizar") == []
    assert not edges.reaches("mizar", "alcor")
