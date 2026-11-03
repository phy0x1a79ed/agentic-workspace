"""Every service folder keeps the plugin contract (install.md R115 to R117).

The contract is redux-core's ``docs/redux/drafts/plugin-contract.md``. Each
check walks the real ``awm/services`` tree, so a new service, a new import or
a new secret read fails here before it reaches a node.
"""

from __future__ import annotations

import ast
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from awm.gateway.hub import discovery

SERVICES = Path(__file__).resolve().parents[2] / "services"
FOLDERS = sorted(p.parent for p in SERVICES.glob("*/run.sh"))

#: Folders that stay on ``service.toml`` until they stop sharing a tool. A
#: manifest plugin may not share a tool across plugins (contract rule 2).
LEGACY = {
    "auth": "publishes into the core's `awm` tool until it moves to an `auth` tool",
    "board": "shares `operations` with reporting until the two folders merge",
    "reporting": "shares `operations` with board until the two folders merge",
    "cx": "shares `reflection` until the harness recut (R135)",
    "keep": "shares `reflection` until the harness recut (R135)",
    "reflection": "shares `reflection` with cx and keep until the harness recut (R135)",
    "search": "search-finish rebuilds it (fleet.md R136)",
}

#: Cross-plugin code reuse that predates R117 item 1. Each pair is debt with
#: an owner. A new pair fails the test.
CROSS_IMPORTS = {
    ("claude-science", "httpsfront"): "fronts its page with httpsfront's certs and proxy",
    ("dsh", "httpsfront"): "fronts its page with httpsfront's certs and proxy",
    ("reporting", "board"): "goes away when board and reporting merge",
    ("scopes", "dvc"): "scopes runs dvc's data_dvc hooks in-process",
}

SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)S?($|_)")
#: A path into the awm tree's services root, as a script spells it. A state
#: folder under ``.awm/services`` is not one.
TREE_PATH = re.compile(r"(?:(?<![.\w])awm|\$\{?AWM\}?)/services/([A-Za-z0-9_-]+)")


def _manifest(folder: Path) -> dict:
    return tomllib.loads((folder / discovery.PLUGIN_MANIFEST).read_text())


def _plugins() -> list[Path]:
    return [f for f in FOLDERS if (f / discovery.PLUGIN_MANIFEST).is_file()]


def _package_of(folder: Path) -> set[str]:
    return {p.name for p in (folder / "awm").iterdir() if p.is_dir() and not p.name.startswith((".", "_"))}


PACKAGES = {pkg: f.name for f in FOLDERS for pkg in _package_of(f)}


def _sources(folder: Path):
    for py in sorted((folder / "awm").rglob("*.py")):
        yield py, ast.parse(py.read_text())


def _constants(folder: Path) -> dict[str, str]:
    out = {}
    for _, tree in _sources(folder):
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                out.update({t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)})
    return out


def _string(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Attribute):
        return consts.get(node.attr)
    return None


def _env_reads(folder: Path) -> set[str]:
    """The environment names the plugin's code reads by a literal or a module constant."""
    consts, names = _constants(folder), set()
    for _, tree in _sources(folder):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                f = node.func
                attr = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                on_environ = isinstance(f, ast.Attribute) and ast.unparse(f.value).endswith("environ")
                if (attr == "get" and on_environ) or attr == "getenv":
                    names.add(_string(node.args[0], consts))
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
                names.add(_string(node.slice, consts))
    return names - {None}


def _imported_plugins(folder: Path) -> set[str]:
    """The other plugins whose ``awm.<package>`` this folder's code imports.

    A library under ``awm/service_components`` is no plugin, so importing it
    passes (R117 item 5). That includes claudedaemon, which the cch harness
    plugin retires (R135).
    """
    found = set()
    for _, tree in _sources(folder):
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for mod in mods:
                parts = mod.split(".")
                if len(parts) > 1 and parts[0] == "awm" and parts[1] in PACKAGES:
                    found.add(PACKAGES[parts[1]])
    return found - {folder.name}


def _script_paths(folder: Path) -> set[str]:
    """The ``awm/services/<name>`` folders that the folder's run.sh and install.sh name in a command."""
    found = set()
    for script in ("run.sh", "install.sh"):
        path = folder / script
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            found |= set(TREE_PATH.findall(line))
    return found


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_each_service_has_a_manifest_or_a_named_legacy_reason(folder):
    has_manifest = (folder / discovery.PLUGIN_MANIFEST).is_file()
    assert has_manifest != (folder.name in LEGACY), folder.name
    if has_manifest:
        assert not (folder / "service.toml").exists(), "the manifest replaces service.toml"


def test_the_loader_refuses_no_folder_of_the_tree(monkeypatch):
    monkeypatch.delenv("AWM_SERVICE_ROOTS", raising=False)
    assert discovery.plugin_refusals() == {}


@pytest.mark.parametrize("folder", _plugins(), ids=lambda f: f.name)
def test_each_manifest_names_its_folder_and_no_host(folder):
    m = _manifest(folder)
    assert "placement" not in m, "the install config places plugins"
    assert m["plugin"]["name"] == folder.name
    assert isinstance(m["plugin"]["version"], str)
    assert folder.name in {s["name"] for s in m["service"]}
    for s in m["service"]:
        for table in s.get("archive", []):
            assert table["mode"] in ("index", "live"), table
    for tool in m["tool"]:
        assert tool["name"] != "awm", "the awm tool belongs to the core"
        assert tool["tier"] in ("on", "off")
    needs = m["needs"]
    assert all(isinstance(x, str) for x in needs["secrets"] + needs["network"])


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_no_plugin_reaches_into_another_plugins_code(folder):
    reached = _imported_plugins(folder) | (_script_paths(folder) - {folder.name})
    unknown = {p for p in reached if (folder.name, p) not in CROSS_IMPORTS}
    assert not unknown, f"{folder.name} imports or installs {sorted(unknown)}: call its tool through the gateway"


def test_the_cross_import_allowlist_holds_no_stale_pair():
    for (owner, used), _ in CROSS_IMPORTS.items():
        folder = SERVICES / owner
        assert used in _imported_plugins(folder) | _script_paths(folder), (owner, used)


@pytest.mark.parametrize("folder", _plugins(), ids=lambda f: f.name)
def test_a_plugins_scripts_find_its_folder_from_where_they_run(folder):
    assert folder.name not in _script_paths(folder), "use the script's own folder, not awm/services/<name>"


@pytest.mark.parametrize("folder", _plugins(), ids=lambda f: f.name)
def test_each_secret_the_code_reads_is_declared(folder):
    reads = {n for n in _env_reads(folder) if SECRET.search(n)}
    missing = reads - set(_manifest(folder)["needs"]["secrets"])
    assert not missing, f"declare {sorted(missing)} in [needs] secrets"


@pytest.mark.parametrize("folder", _plugins(), ids=lambda f: f.name)
def test_each_plugin_loads_the_same_from_a_root_outside_the_tree(folder, tmp_path, monkeypatch, awm_workspace):
    monkeypatch.setenv("AWM_SERVICES_DIR", str(SERVICES))
    monkeypatch.delenv("AWM_SERVICE_ROOTS", raising=False)
    tool, tier, archive = discovery.read_tool(folder), discovery.read_tier(folder), discovery.read_archive(folder)
    outside = tmp_path / "elsewhere"
    copy = outside / folder.name
    copy.mkdir(parents=True)
    for name in ("run.sh", "install.sh", discovery.PLUGIN_MANIFEST):
        if (folder / name).is_file():
            shutil.copy2(folder / name, copy / name)
    empty = tmp_path / "no-services"
    empty.mkdir()
    for dep in _manifest(folder)["needs"].get("tool", []):
        stub = empty / f"stub-{dep['name']}"
        stub.mkdir()
        (stub / "run.sh").write_text("#!/bin/bash\n")
        (stub / discovery.PLUGIN_MANIFEST).write_text(
            f"[plugin]\nname = 'stub-{dep['name']}'\n[[service]]\nname = 'stub-{dep['name']}'\n"
            f"[[tool]]\nname = '{dep['name']}'\nmajor = {dep['major']}\n")
    monkeypatch.setenv("AWM_SERVICES_DIR", str(empty))
    monkeypatch.setenv("AWM_SERVICE_ROOTS", str(outside))
    monkeypatch.setenv("AWM_PROFILES", ",".join(_manifest(folder)["service"][0].get("profiles", [])))
    spec = discovery.discover_service(folder.name)
    if tool in discovery.RESERVED_TOOLS:
        assert spec is None, "a reserved name loads only from a core root"
        return
    assert spec is not None and spec.enabled
    assert spec.cwd == str(copy)
    assert discovery.service_tool(folder.name) == tool
    assert discovery.service_tier(folder.name) == tier
    assert discovery.service_archive(folder.name) == archive
