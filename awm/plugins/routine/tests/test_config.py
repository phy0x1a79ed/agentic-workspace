"""The configs the land batch reads: deploy.toml, the install config's [fleet] table, pins."""

from __future__ import annotations

import pytest

from awm.routine import deployconf, fleetconf

pytestmark = pytest.mark.smoke

# A fixture install config: the real one is the cloud project's (fleet.md R140).
INSTALL = """
plugins = ["*"]

[fleet]
home = "mizar"
nodes = ["mizar", "alcor", "capella"]
"""


@pytest.mark.parametrize("text, msg", [
    ("[refresh]\nkind = 'restart'", "unknown keys"),
    ("[fixer]\npeer = 'x'", "unknown section"),
    ("[fleet]\nnodes = ['a']", "install config"),
    ("[deploy]\nnode = 'x'", "unknown keys"),
    ("[mesh]\n", "unknown section"),
])
def test_deployconf_rejects(text, msg):
    with pytest.raises(deployconf.ConfigError, match=msg):
        deployconf.parse(text)


def test_deployconf_refresh():
    r = deployconf.parse('[refresh]\nsync = "s"\ncommand = "c"\n').refresh
    assert (r.sync, r.command) == ("s", "c")


def test_fleetconf_reads_home_and_overrides():
    f = fleetconf.parse(INSTALL + '[fleet.project.engcyc]\ncanonical = "altair"\n'
                        'nodes = ["altair"]\n')
    assert f.canonical("awm") == "mizar"
    assert f.rollout("awm") == ("mizar", "alcor", "capella")
    assert (f.canonical("engcyc"), f.rollout("engcyc")) == ("altair", ("altair",))


def test_fleetconf_absent_is_this_node_alone(tmp_path):
    f = fleetconf.load(tmp_path / "config.toml")
    assert f.canonical("awm") == "" and f.rollout("awm") == ()


@pytest.mark.parametrize("text, msg", [
    ('[fleet]\nhome = "a"\nnodes = ["b", "a"]', "must start with 'a'"),
    ('[fleet]\nnodes = ["a", "a"]', "twice"),
    ('[fleet]\nhomebase = "a"', "unknown keys"),
    ('[fleet.project.p]\ncanonical = "b"\nnodes = ["a", "b"]', "must start with 'b'"),
    ('[fleet.project.p]\npeer = "b"', "only canonical and nodes"),
])
def test_fleetconf_rejects(text, msg):
    with pytest.raises(fleetconf.FleetConfigError, match=msg):
        fleetconf.parse(text)


PUSH = INSTALL + '\n[fleet.push.alcor]\nurl = "{url}"\nprojects = ["proj"]\nrefresh = "true"\n'


def test_fleetconf_push():
    f = fleetconf.parse(PUSH.format(url="altair:/w/projects/{project}/.bare"))
    assert f.push_url("alcor", "proj") == "altair:/w/projects/proj/.bare"
    assert f.push_url("alcor", "other") == "" and f.push_url("capella", "proj") == ""


@pytest.mark.parametrize("text, msg", [
    ('[fleet.push.a]\nurl = "a:/x"\nprojects = ["p"]\nrefresh = "true"', "with {project}"),
    ('[fleet.push.a]\nurl = "a:/{project}"\nrefresh = "true"', "url and projects"),
    ('[fleet.push.a]\nurl = "a:/{project}"\nprojects = "p"\nrefresh = "true"', "list of project names"),
])
def test_fleetconf_push_rejects(text, msg):
    with pytest.raises(fleetconf.FleetConfigError, match=msg):
        fleetconf.parse(text)


def test_fleetconf_push_without_its_own_refresh():
    f = fleetconf.parse('[fleet.push.a]\nurl = "a:/{project}"\nprojects = ["p"]\n')
    assert f.push_url("a", "p") == "a:/p" and f.refresh_command("a") == ""


PUSH_ART = PUSH + 'artifacts = "{dest}"\n'
ART_ONLY = INSTALL + '\n[fleet.push.alcor]\nartifacts = "{dest}"\n'


def test_fleetconf_sleepers():
    f = fleetconf.parse('[fleet]\nhome = "mizar"\nnodes = ["mizar", "capella"]\nsleepers = ["capella"]\n')
    assert f.sleepers == ("capella",)
    with pytest.raises(fleetconf.FleetConfigError, match="names home"):
        fleetconf.parse('[fleet]\nhome = "mizar"\nsleepers = ["mizar"]\n')


def test_fleetconf_artifacts_only():
    f = fleetconf.parse(ART_ONLY.format(dest="alcor:/w/.awm/artifacts"))
    assert f.artifacts_dest("alcor") == "alcor:/w/.awm/artifacts"
    assert f.push_url("alcor", "proj") == ""
    for bad in ('[fleet.push.a]\nprojects = ["p"]\nartifacts = "a:/x"',
                '[fleet.push.a]\nartifacts = ""', '[fleet.push.a]\n'):
        with pytest.raises(fleetconf.FleetConfigError):
            fleetconf.parse(bad)


def test_fleetconf_push_artifacts():
    f = fleetconf.parse(PUSH_ART.format(url="d:/w/projects/{project}/.bare", dest="d:/w/.awm/artifacts"))
    assert f.artifacts_dest("alcor") == "d:/w/.awm/artifacts" and f.artifacts_dest("capella") == ""


def test_deployconf_artifact_pins():
    assert deployconf.parse('[artifacts]\npins = ["a/b.pin"]').artifact_pins == ("a/b.pin",)
    with pytest.raises(deployconf.ConfigError, match="list of paths"):
        deployconf.parse('[artifacts]\npins = "a/b.pin"')


@pytest.mark.parametrize("text, msg", [
    ("version=v1\nsha256=ab\n", "needs file and sha256"),
    ("file=../x.tar.xz\nsha256=ab\n", "bare file name"),
])
def test_a_pin_names_a_bare_file_and_its_hash(text, msg):
    from awm.routine import artifacts
    with pytest.raises(ValueError, match=msg):
        artifacts.parse_pin(text, "svc/server.pin")
