"""awm's own deploy.toml and its scripts, and the full-tests known-failures script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awm.routine import definitions, deployconf

from tests.conftest import git

pytestmark = pytest.mark.smoke

REPO = Path(__file__).resolve().parents[4]


def test_awm_deploy_toml_parses():
    cfg = deployconf.parse((REPO / "deploy.toml").read_text())
    assert cfg.check_timeout == 600 and "--smoke" in cfg.check
    assert cfg.refresh.sync == "scripts/refresh-checkout.sh"
    assert cfg.refresh.command == "scripts/node-refresh.sh"
    assert (REPO / cfg.refresh.sync).is_file() and (REPO / cfg.refresh.command).is_file()
    assert cfg.backup == "github"


def _node(tmp_path):
    """A canonical bare repo with refs/mizar/release, and a node checkout of it."""
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "release", str(seed))
    (seed / "keep.txt").write_text("1\n")
    (seed / "gone.txt").write_text("1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "one")
    bare = tmp_path / "bare"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    node = tmp_path / "node"
    git(tmp_path, "clone", "-q", "-o", "awm", "-b", "release", str(bare), str(node))
    (seed / "gone.txt").unlink()
    (seed / "keep.txt").write_text("2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "two")
    to = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "-q", str(bare), "HEAD:refs/mizar/release")
    (node / ".awm").mkdir()
    (node / ".awm" / "state.db").write_text("x")
    (node / "stray.txt").write_text("x")
    return node, to


def _refresh(node, to):
    return subprocess.run(["sh", str(REPO / "scripts/refresh-checkout.sh")], cwd=node,
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                               "AWM_REFRESH_TO": to})


def test_refresh_checkout_takes_a_pushed_release(tmp_path):
    node, to = _node(tmp_path)
    (node / ".gitignore").write_text(".awm/\n")
    r = _refresh(node, to)
    assert r.returncode == 0, r.stderr
    assert git(node, "rev-parse", "HEAD") == to
    assert (node / "keep.txt").read_text() == "2\n" and not (node / "gone.txt").exists()
    assert not (node / "stray.txt").exists() and (node / ".awm" / "state.db").exists()


def test_refresh_checkout_keeps_a_local_commit(tmp_path):
    node, to = _node(tmp_path)
    git(node, "commit", "-q", "--allow-empty", "-m", "hand patch")
    head = git(node, "rev-parse", "HEAD")
    r = _refresh(node, to)
    assert r.returncode == 1 and "does not contain" in r.stderr
    assert git(node, "rev-parse", "HEAD") == head


def test_awm_deploy_toml_names_no_host():
    # fleet.md R140: the node list lives in each node's install config.
    assert "[fleet]" not in (REPO / "deploy.toml").read_text()


FAKE_RUN_TESTS = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in --summary) out="$2"; shift 2 ;; *) shift ;; esac
done
cat "$(git rev-parse --show-toplevel)/results" > "$out"
"""


def test_full_tests_flags_only_new_failures(tmp_path):
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts/full-tests.sh", repo / "scripts/full-tests.sh")
    fake = repo / "scripts/run-tests.sh"
    fake.write_text(FAKE_RUN_TESTS)
    fake.chmod(0o755)
    git(repo, "init", "-q")
    (repo / "results").write_text("a PASS\nb FAIL (rc=1)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")

    def run():
        return subprocess.run(["bash", "scripts/full-tests.sh"], cwd=repo,
                              capture_output=True, text=True)

    first = run()
    assert first.returncode == 1 and "new failures: b" in first.stdout
    head = git(repo, "rev-parse", "HEAD")
    known = repo / ".awm/full-tests/known-failures"
    assert known.read_text().split() == ["b", head]
    # The same failure the next night is known, not new.
    second = run()
    assert second.returncode == 0 and "no new failures" in second.stdout
    # b recovers, a breaks: only a is new, and b leaves the known list.
    (repo / "results").write_text("a FAIL (rc=1)\nb PASS\n")
    third = run()
    assert third.returncode == 1 and "new failures: a" in third.stdout
    assert known.read_text().split()[0] == "a"


def _move(ws, *args):
    return subprocess.run(["bash", str(REPO / "scripts/move-canonical.sh"), *args],
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                               "AWM_WORKSPACE": str(ws)})


def test_move_canonical_repoints_the_checkout_and_adds_github(tmp_path):
    old = tmp_path / "old.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "release", str(seed))
    git(seed, "commit", "-q", "--allow-empty", "-m", "one")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(old))
    ws = tmp_path / "ws"
    git(tmp_path, "clone", "-q", "-o", "awm", str(old), str(ws))
    git(ws, "remote", "add", "origin", str(old))
    git(ws, "remote", "add", "other", str(seed))
    bare = ws / "projects/awm/.bare"
    git(tmp_path, "clone", "-q", "--bare", str(old), str(bare))
    r = _move(ws, "repoint", str(old), "local")
    assert r.returncode == 0, r.stderr
    assert git(ws, "remote", "get-url", "awm") == str(bare)
    assert git(ws, "remote", "get-url", "origin") == str(bare)
    assert git(ws, "remote", "get-url", "other") == str(seed)
    gh = tmp_path / "gh.git"
    git(tmp_path, "init", "-q", "--bare", str(gh))
    r = _move(ws, "github", str(gh))
    assert r.returncode == 0 and "first deploy" in r.stdout
    git(bare, "push", "-q", "github", "release")
    git(seed, "commit", "-q", "--allow-empty", "-m", "made on github only")
    git(seed, "push", "-q", str(gh), "release")
    r = _move(ws, "github", str(gh))
    assert r.returncode == 1 and "stops at backup" in r.stderr
    assert "canonical_repos" in _move(ws, "order").stdout


# -- the core binary pin: scripts/core.pin and scripts/awm-deploy.sh ------------

def test_awm_deploy_toml_pins_the_built_files():
    cfg = deployconf.parse((REPO / "deploy.toml").read_text())
    assert {"scripts/core.pin", "scripts/pages.pin"} <= set(cfg.artifact_pins)
    assert cfg.artifact_refresh == "scripts/refresh-pins.sh" and cfg.artifact_refresh_timeout > 600
    from awm.routine import artifacts
    pin = artifacts.parse_pin((REPO / "scripts/core.pin").read_text(), "scripts/core.pin")
    assert pin.file.startswith("awm-core-") and len(pin.sha256) == 64


FAKE_BIN = '#!/bin/sh\necho "fake awm $*"\n'
PAGE = "awm/services/svc/pages/ui/dist"


def _commit(node, msg):
    git(node, "add", "-A")
    git(node, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


def _pin(node, name, version, file, blob, sha=None):
    import hashlib
    (node / "scripts" / name).write_text(
        f"version={version}\nfile={file}\nsha256={sha or hashlib.sha256(blob).hexdigest()}\n")


def _pages_tar(tmp_path, body: str) -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for d in ("awm", "awm/services", "awm/services/svc", "awm/services/svc/pages",
                  "awm/services/svc/pages/ui", PAGE):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            t.addfile(info)
        data = body.encode()
        info = tarfile.TarInfo(f"{PAGE}/index.html")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _deploy_node(tmp_path, *, core_sha=None, pages_sha=None, core_changed=False, pages_changed=False):
    """A checkout holding awm-deploy.sh and both pins, and the artifact store its node holds."""
    node = tmp_path / "node"
    (node / "scripts").mkdir(parents=True)
    (node / "core").mkdir()
    (node / "awm/services/svc/pages/ui").mkdir(parents=True)
    for f in ("awm-deploy.sh", "pins.sh"):
        shutil.copy(REPO / "scripts" / f, node / "scripts" / f)
    (node / "core/Cargo.toml").write_text("x\n")
    (node / ".gitignore").write_text("awm/*/*/pages/*/dist/\n")
    (node / "awm/services/svc/pages/ui/index.html").write_text("src\n")
    git(tmp_path, "init", "-q", str(node))
    _commit(node, "one")
    store = node / ".awm/artifacts"
    store.mkdir(parents=True)

    def pins_sh(cmd):
        return subprocess.run(["bash", "-c", f". scripts/pins.sh; {cmd} ."], cwd=node,
                              capture_output=True, text=True, check=True).stdout.strip()

    cv, pv = pins_sh("core_version"), pins_sh("pages_version")
    pages = _pages_tar(tmp_path, "built\n")
    _pin(node, "core.pin", cv, f"awm-{cv}", FAKE_BIN.encode(), core_sha)
    _pin(node, "pages.pin", pv, f"awm-{pv}.tar.gz", pages, pages_sha)
    (store / f"awm-{cv}").write_bytes(FAKE_BIN.encode())
    (store / f"awm-{pv}.tar.gz").write_bytes(pages)
    stale = node / PAGE
    stale.mkdir(parents=True)
    (stale / "old.js").write_text("old\n")
    if core_changed:
        (node / "core/Cargo.toml").write_text("y\n")
    if pages_changed:
        (node / "awm/services/svc/pages/ui/index.html").write_text("changed\n")
    _commit(node, "two")
    return node


def _run_deploy(node, *args):
    return subprocess.run(["bash", str(node / "scripts/awm-deploy.sh"), *args], cwd=node,
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]})


def test_awm_deploy_runs_the_pinned_binary_and_unpacks_the_pages(tmp_path):
    node = _deploy_node(tmp_path)
    r = _run_deploy(node, "--dry-run")
    assert r.returncode == 0, r.stderr
    file = (node / "scripts/core.pin").read_text().split("file=")[1].splitlines()[0]
    assert r.stdout.strip() == (f"fake awm deploy --root {node} --bin {node}/.awm/artifacts/{file}"
                                " --no-build --dry-run")
    assert (node / PAGE / "index.html").read_text() == "built\n"
    assert not (node / PAGE / "old.js").exists()


@pytest.mark.parametrize("kw, msg", [
    ({"core_sha": "0" * 64}, "has sha256"),
    ({"pages_sha": "0" * 64}, "has sha256"),
    ({"core_changed": True}, "refresh-pins.sh"),
    ({"pages_changed": True}, "refresh-pins.sh"),
])
def test_awm_deploy_stops_on_a_bad_pin_and_changes_nothing(tmp_path, kw, msg):
    node = _deploy_node(tmp_path, **kw)
    r = _run_deploy(node)
    assert r.returncode == 1 and msg in r.stderr
    assert r.stdout == "" and (node / PAGE / "old.js").exists()


def test_awm_deploy_stops_when_an_artifact_never_arrived(tmp_path):
    node = _deploy_node(tmp_path)
    for f in (node / ".awm/artifacts").glob("*.tar.gz"):
        f.unlink()
    r = _run_deploy(node)
    assert r.returncode == 1 and "pushes it to this node" in r.stderr


async def test_pushed_artifacts_deploy_on_the_fixture_node(tmp_path):
    from awm.routine import artifacts
    node = _deploy_node(tmp_path)
    mizar = tmp_path / "mizar-store"
    mizar.mkdir()
    for name in ("core.pin", "pages.pin"):
        pin = artifacts.parse_pin((node / "scripts" / name).read_text(), f"scripts/{name}")
        shutil.move(node / ".awm/artifacts" / pin.file, mizar / pin.file)
        await artifacts.check(mizar, pin)
        res = await artifacts.push(mizar, pin, str(node / ".awm/artifacts"))
        assert res.ok, res.output
    r = _run_deploy(node)
    assert r.returncode == 0 and r.stdout.startswith("fake awm deploy --root")
    assert (node / PAGE / "index.html").read_text() == "built\n"


# -- awm/scripts/build.sh runs npm ci on a changed lock; awm/install.sh asks `awm wants` ---

def _tree(tmp_path):
    """A checkout holding copies of the two scripts, for them to run against."""
    tree = tmp_path / "tree"
    (tree / "awm/scripts").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(tree))
    for rel in ("awm/scripts/build.sh", "awm/install.sh"):
        shutil.copy(REPO / rel, tree / rel)
    return tree


def _fake_npm(tmp_path):
    """An `npm` whose `ci` installs a fake vite and logs the call."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text('#!/bin/sh\necho "npm $*" >> "$NPM_LOG"\nrm -rf node_modules\n'
                   'mkdir -p node_modules/.bin\nprintf "#!/bin/sh\\n" > node_modules/.bin/vite\n'
                   'chmod +x node_modules/.bin/vite\n')
    npm.chmod(0o755)
    return bin_dir


def _build(tree, bin_dir, log):
    return subprocess.run(["bash", str(tree / "awm/scripts/build.sh")], cwd=tree,
                          capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": os.environ["HOME"],
                               "NPM_LOG": str(log)})


def test_build_runs_npm_ci_only_when_the_lock_changed(tmp_path):
    tree, log = _tree(tmp_path), tmp_path / "npm.log"
    bin_dir = _fake_npm(tmp_path)
    (tree / "awm/package-lock.json").write_text("one\n")
    assert _build(tree, bin_dir, log).returncode == 0
    assert log.read_text() == "npm ci\n"
    assert _build(tree, bin_dir, log).returncode == 0
    assert log.read_text() == "npm ci\n"
    (tree / "awm/package-lock.json").write_text("two\n")
    assert _build(tree, bin_dir, log).returncode == 0
    assert log.read_text() == "npm ci\nnpm ci\n"


def _install(tree, core, state):
    return subprocess.run(["bash", str(tree / "awm/install.sh")], cwd=tree,
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                               "AWM_CORE": str(core), "AWM_DIR": str(state)})


def _fake_core(tmp_path):
    """A core whose `wants` follows config.toml's exclude line, as Config::wants does."""
    core = tmp_path / "core"
    core.write_text('#!/bin/sh\n[ "$1" = wants ] || exit 2\n'
                    'grep -q "\\"$2\\"" "$AWM_DIR/config.toml" && exit 1\nexit 0\n')
    core.chmod(0o755)
    return core


@pytest.mark.skipif(not shutil.which("socat"), reason="needs socat")
def test_install_runs_the_plugins_the_config_enables(tmp_path):
    tree, state = _tree(tmp_path), tmp_path / "state"
    state.mkdir()
    (state / "config.toml").write_text('plugins = ["*"]\nexclude = ["off"]\n')
    for name in ("on", "off"):
        d = tree / "awm/services" / name
        d.mkdir(parents=True)
        (d / "install.sh").write_text(f'echo ran >> "{tmp_path}/{name}.ran"\n')
    r = _install(tree, _fake_core(tmp_path), state)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "on.ran").exists() and not (tmp_path / "off.ran").exists()
    assert "does not enable it): off" in r.stdout


@pytest.mark.skipif(not shutil.which("socat"), reason="needs socat")
def test_install_stops_when_the_core_cannot_answer(tmp_path):
    tree, state = _tree(tmp_path), tmp_path / "state"
    state.mkdir()
    d = tree / "awm/services/on"
    d.mkdir(parents=True)
    (d / "install.sh").write_text(f'echo ran >> "{tmp_path}/on.ran"\n')
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 2\n")
    broken.chmod(0o755)
    r = _install(tree, broken, state)
    assert r.returncode == 1 and "wants on' failed" in r.stderr and not (tmp_path / "on.ran").exists()


# -- scripts/refresh-pins.sh: rebuild only the stale pin, with the caches kept ----------

def _refresh_tree(tmp_path):
    """A checkout holding the real pins.sh and refresh-pins.sh, and stub release scripts
    that log their call and write a real pin and artifact."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "core").mkdir()
    (tree / "awm/services/svc/pages/ui").mkdir(parents=True)
    for f in ("refresh-pins.sh", "pins.sh"):
        shutil.copy(REPO / "scripts" / f, tree / "scripts" / f)
    stub = '''set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
. "$root/scripts/pins.sh"
mkdir -p "$AWM_DIR"; echo "%(name)s target=${AWM_CARGO_TARGET_DIR:-} nm=$(ls "$root/awm/node_modules" 2>/dev/null | tr '\\n' ' ')" >> "$AWM_DIR/calls.log"
version="$(%(ver)s "$root")"
mkdir -p "$AWM_DIR/artifacts"; echo "%(name)s" > "$AWM_DIR/artifacts/$version"
%(extra)s
write_pin "$root/scripts/%(name)s.pin" d "$version" "$version" "$AWM_DIR/artifacts"
'''
    (tree / "scripts/release-core.sh").write_text(stub % {"name": "core", "ver": "core_version", "extra": ""})
    (tree / "scripts/release-pages.sh").write_text(stub % {
        "name": "pages", "ver": "pages_version",
        "extra": 'mkdir -p "$root/awm/node_modules/pkg"; echo ok > "$root/awm/node_modules/pkg/index.js"'})
    for f in ("release-core.sh", "release-pages.sh"):
        (tree / "scripts" / f).chmod(0o755)
    (tree / "core/Cargo.toml").write_text("1\n")
    (tree / "awm/package-lock.json").write_text("{}\n")
    (tree / "awm/services/svc/pages/ui/index.html").write_text("1\n")
    (tree / ".gitignore").write_text("node_modules\n.awm/\n")
    git(tmp_path, "init", "-q", str(tree))
    _commit(tree, "one")
    return tree


def _run_pins(tree, awm_dir):
    r = subprocess.run(["bash", str(tree / "scripts/refresh-pins.sh")], cwd=tree, capture_output=True,
                       text=True, env={**os.environ, "AWM_DIR": str(awm_dir)})
    assert r.returncode == 0, r.stderr
    log = awm_dir / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_refresh_pins_builds_what_is_stale_and_nothing_else(tmp_path):
    tree, awm_dir = _refresh_tree(tmp_path), tmp_path / "awm-dir"
    first = _run_pins(tree, awm_dir)
    assert [c.split()[0] for c in first] == ["core", "pages"]
    assert f"target={awm_dir}/build-cache/cargo-target" in first[0]
    assert (tree / "scripts/core.pin").read_text().count("version=core-") == 1
    # Nothing changed, so nothing builds.
    assert _run_pins(tree, awm_dir) == first
    # A core change rebuilds the core only.
    (tree / "core/Cargo.toml").write_text("2\n")
    _commit(tree, "core")
    assert [c.split()[0] for c in _run_pins(tree, awm_dir)[2:]] == ["core"]
    # A page change rebuilds the pages only, and a missing artifact rebuilds its pin.
    (tree / "awm/services/svc/pages/ui/index.html").write_text("2\n")
    _commit(tree, "page")
    assert [c.split()[0] for c in _run_pins(tree, awm_dir)[3:]] == ["pages"]
    for f in (awm_dir / "artifacts").iterdir():
        f.unlink()
    assert [c.split()[0] for c in _run_pins(tree, awm_dir)[4:]] == ["core", "pages"]


def test_refresh_pins_reuses_node_modules_by_lock_in_a_fresh_tree(tmp_path):
    tree, awm_dir = _refresh_tree(tmp_path), tmp_path / "awm-dir"
    _run_pins(tree, awm_dir)
    cached = list((awm_dir / "build-cache/node_modules").iterdir())
    assert len(cached) == 1 and (cached[0] / "pkg/index.js").is_file()
    # A new temp tree has no node_modules; the cache brings it back before the page build.
    shutil.rmtree(tree / "awm/node_modules")
    (tree / "awm/services/svc/pages/ui/index.html").write_text("2\n")
    _commit(tree, "page")
    assert "nm=pkg" in _run_pins(tree, awm_dir)[-1]
    # A new lock replaces the cached tree.
    (tree / "awm/package-lock.json").write_text('{"v":2}\n')
    shutil.rmtree(tree / "awm/node_modules")
    _commit(tree, "lock")
    assert "nm=pkg" not in _run_pins(tree, awm_dir)[-1]
    assert [p.name for p in (awm_dir / "build-cache/node_modules").iterdir()] != [cached[0].name]
