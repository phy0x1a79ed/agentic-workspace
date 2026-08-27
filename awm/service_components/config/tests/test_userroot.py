"""userroot resolves an edge identity to a worktree, or to "legacy"; strict
mode refuses instead. autocommit touches only the service's own subdirectory."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from awm.config import autocommit, userroot

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.config.SERVICES_DIR", tmp_path / ".awm" / "services")
    monkeypatch.delenv(userroot.TEMPLATE_ENV, raising=False)
    monkeypatch.delenv(userroot.STRICT_ENV, raising=False)
    (tmp_path / "projects" / "userdata" / "tony").mkdir(parents=True)
    return tmp_path


def test_user_of_accepts_only_well_formed_names():
    assert userroot.user_of("user:tony") == "tony"
    assert userroot.user_of("user:Tony") is None
    assert userroot.user_of("user:../x") is None
    assert userroot.user_of("scope-slug") is None
    assert userroot.user_of(None) is None


def test_resolve_known_unknown_and_strict(ws, monkeypatch):
    assert userroot.resolve("user:tony") == "tony"
    assert userroot.resolve("user:steven") is None
    assert userroot.resolve("user:operator") is None
    assert userroot.resolve("placement-abc") is None
    assert userroot.root_for("tony") == ws / "projects" / "userdata" / "tony"
    with pytest.raises(userroot.UnknownUser):
        userroot.root_for("steven")
    monkeypatch.setenv(userroot.STRICT_ENV, "1")
    assert userroot.resolve("user:tony") == "tony"
    with pytest.raises(PermissionError):
        userroot.resolve("user:steven")
    with pytest.raises(PermissionError):
        userroot.resolve(None)


def test_template_override_and_users_listing(ws, monkeypatch):
    alt = ws / "alt"
    (alt / "steven").mkdir(parents=True)
    (alt / "Bad Name").mkdir()
    monkeypatch.setenv(userroot.TEMPLATE_ENV, str(alt / "{user}"))
    assert userroot.users() == ["steven"]
    assert userroot.resolve("user:tony") is None
    assert userroot.root_for("steven") == alt / "steven"


def test_state_dir_and_bind(ws):
    d = userroot.state_dir("notes", "tony")
    assert d == ws / ".awm" / "services" / "notes" / "users" / "tony" and d.is_dir()
    assert userroot.current() is None
    with userroot.bind("tony"):
        assert userroot.current() == "tony"
    assert userroot.current() is None


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), *autocommit.GIT_IDENTITY, "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), *autocommit.GIT_IDENTITY, "commit", "-qm", "init"], check=True)
    return path


def _log(path: Path, n=1) -> str:
    return subprocess.run(["git", "-C", str(path), "log", f"-{n}", "--format=%B", "--name-only"],
                          capture_output=True, text=True, check=True).stdout


def test_commit_subdir_touches_only_its_subdir(tmp_path):
    root = _repo(tmp_path / "wt")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("hello")
    (root / "drawio").mkdir()
    (root / "drawio" / "half.drawio").write_text("<partial>")
    sha = autocommit.commit_subdir(root, "notes", "tony", "notes: flush")
    assert sha
    out = _log(root)
    assert "Author-Handle: user:tony" in out
    assert "notes/a.md" in out and "drawio" not in out
    assert autocommit.commit_subdir(root, "notes", "tony", "again") is None
    (root / "notes" / "a.md").unlink()
    assert autocommit.commit_subdir(root, "notes", "tony", "delete") is not None


@pytest.mark.skipif(autocommit.dvc_bin() is None, reason="dvc not installed")
def test_pin_figures_when_they_move(tmp_path, monkeypatch):
    root = _repo(tmp_path / "wt")
    dvc = autocommit.dvc_bin()
    subprocess.run([dvc, "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-C", str(root), *autocommit.GIT_IDENTITY, "commit", "-qam", "dvc"], check=True)
    fig = root / "data" / "figures"
    fig.mkdir(parents=True)
    (fig / "a.png").write_bytes(b"png")
    sha = autocommit.pin_figures(root, "tony")
    assert sha and (root / "data" / "figures.dvc").is_file()
    assert "figures.dvc" in _log(root)
    assert autocommit.pin_figures(root, "tony") is None
