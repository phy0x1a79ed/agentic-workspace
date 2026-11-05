import sys
from pathlib import Path

from awm.config import tools


def _conda_base(root: Path, *envs: str) -> Path:
    (root / "conda-meta").mkdir(parents=True)
    for env in envs:
        exe = root / "envs" / env / "bin" / env
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return root


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    for var in ("AWM_DVC_BIN", "AWM_CONDA_ROOT", "MAMBA_ROOT_PREFIX", "CONDA_EXE", "MAMBA_EXE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "no-conda"))


def test_the_base_comes_from_the_running_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = _conda_base(tmp_path / "forge", "dvc", "awm")
    monkeypatch.setattr(sys, "prefix", str(base / "envs" / "awm"))
    assert tools.dvc_bin() == str(base / "envs" / "dvc" / "bin" / "dvc")


def test_the_base_comes_from_the_conda_launcher(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = _conda_base(tmp_path / "forge", "dvc")
    monkeypatch.setenv("MAMBA_EXE", str(base / "condabin" / "mamba"))
    assert tools.dvc_bin() == str(base / "envs" / "dvc" / "bin" / "dvc")


def test_the_override_wins_and_a_dead_override_falls_through(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = _conda_base(tmp_path / "forge", "dvc")
    monkeypatch.setenv("AWM_CONDA_ROOT", str(base))
    fake = tmp_path / "fake-dvc"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AWM_DVC_BIN", str(fake))
    assert tools.dvc_bin() == str(fake)
    monkeypatch.setenv("AWM_DVC_BIN", str(tmp_path / "gone"))
    assert tools.dvc_bin() == str(base / "envs" / "dvc" / "bin" / "dvc")


def test_a_tool_installed_later_is_found(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = _conda_base(tmp_path / "forge")
    monkeypatch.setenv("AWM_CONDA_ROOT", str(base))
    assert tools.dvc_bin() is None
    _conda_base(tmp_path / "later", "dvc")
    (tmp_path / "later" / "envs").rename(base / "envs")
    assert tools.dvc_bin() == str(base / "envs" / "dvc" / "bin" / "dvc")
