"""Every plugin folder keeps the plugin contract (install.md R115 to R117).

The contract is ``docs/redux/drafts/plugin-contract.md``. Each check walks the real
``awm/services`` and ``awm/plugins`` roots, so a new plugin, a new import or a new
secret read fails here before it reaches a node. That the loader refuses no folder of
the tree is the Rust test ``core/crates/awm-gateway/tests/tree.rs``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

AWM = Path(__file__).resolve().parents[2] / "awm"
MANIFEST = "awm-plugin.toml"
#: Every folder of the two core roots that holds Python code: a plugin or a folder the
#: loader ignores, whose package a plugin must not import either.
CODE = sorted(p.parent for root in ("services", "plugins") for p in (AWM / root).glob("*/awm") if p.is_dir())
FOLDERS = sorted(p.parent for root in ("services", "plugins") for p in (AWM / root).glob(f"*/{MANIFEST}"))


#: What the gateway sets for every service: no plugin declares it.
GATEWAY_ENV = {"AWM_HUB_TOKEN_FILE"}
SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE)S?($|_)")
#: A path into the awm tree's services root, as a script spells it. A state
#: folder under ``.awm/services`` is not one.
TREE_PATH = re.compile(r"(?:(?<![.\w])awm|\$\{?AWM\}?)/((?:services|plugins)/[A-Za-z0-9_-]+)")


def _manifest(folder: Path) -> dict:
    return tomllib.loads((folder / MANIFEST).read_text())


def _package_of(folder: Path) -> set[str]:
    return {p.name for p in (folder / "awm").iterdir() if p.is_dir() and not p.name.startswith((".", "_"))}


PACKAGES = {pkg: f.name for f in CODE for pkg in _package_of(f)}


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
    passes (R117 item 5).
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
    """The ``awm/services/<name>`` and ``awm/plugins/<name>`` folders that the folder's run.sh and install.sh name in a command."""
    found = set()
    for script in ("run.sh", "install.sh"):
        path = folder / script
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for folder_path in TREE_PATH.findall(line):
                found.add(folder_path.split("/", 1)[1])
    return found


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_no_python_plugin_names_an_install_hook(folder):
    """awm/install.sh runs a Python plugin's install.sh in the awm env. The gateway would run an
    `install` key's hook without conda on PATH, fail it and refuse the plugin."""
    assert all("install" not in s for s in _manifest(folder).get("service", [])), \
        "drop the install key: awm/install.sh runs install.sh"


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_each_manifest_names_its_folder_and_no_host(folder):
    m = _manifest(folder)
    assert "placement" not in m, "the install config places plugins"
    assert m["plugin"]["name"] == folder.name
    assert isinstance(m["plugin"]["version"], str)
    services = m.get("service", [])
    assert services or m.get("page"), "a plugin runs a service or serves a page"
    if services:
        assert folder.name in {s["name"] for s in services}
    for s in services:
        for table in s.get("archive", []):
            assert table["mode"] in ("index", "live"), table
    for tool in m.get("tool", []):
        assert tool["name"] != "awm", "the awm tool belongs to the core"
        assert tool["tier"] in ("on", "off")
    needs = m.get("needs", {})
    assert all(isinstance(x, str) for x in needs.get("secrets", []) + needs.get("network", []))


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_no_plugin_reaches_into_another_plugins_code(folder):
    reached = _imported_plugins(folder) | (_script_paths(folder) - {folder.name})
    assert not reached, f"{folder.name} imports or installs {sorted(reached)}: call its tool through the gateway"


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_a_plugins_scripts_find_its_folder_from_where_they_run(folder):
    assert folder.name not in _script_paths(folder), "use the script's own folder, not awm/<root>/<name>"


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda f: f.name)
def test_each_secret_the_code_reads_is_declared(folder):
    reads = {n for n in _env_reads(folder) if SECRET.search(n)} - GATEWAY_ENV
    missing = reads - set(_manifest(folder).get("needs", {}).get("secrets", []))
    assert not missing, f"declare {sorted(missing)} in [needs] secrets"
