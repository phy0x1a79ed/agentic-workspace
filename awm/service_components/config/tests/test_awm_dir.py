"""AWM_DIR comes from the environment when set, else from the workspace."""

from __future__ import annotations

import importlib
from pathlib import Path


def _reload(monkeypatch, **env):
    for k in ("AWM_DIR", "AWM_WORKSPACE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import awm.config as config
    return importlib.reload(config)


def test_awm_dir_env_wins(monkeypatch, tmp_path: Path):
    config = _reload(monkeypatch, AWM_DIR=str(tmp_path / "state"), AWM_WORKSPACE=str(tmp_path / "ws"))
    assert config.AWM_DIR == (tmp_path / "state").resolve()
    assert config.SERVICES_DIR == (tmp_path / "state" / "services").resolve()


def test_awm_dir_defaults_to_the_workspace(monkeypatch, tmp_path: Path):
    config = _reload(monkeypatch, AWM_WORKSPACE=str(tmp_path))
    assert config.AWM_DIR == tmp_path.resolve() / ".awm"


def test_blank_awm_dir_is_unset(monkeypatch, tmp_path: Path):
    config = _reload(monkeypatch, AWM_DIR=" ", AWM_WORKSPACE=str(tmp_path))
    assert config.AWM_DIR == tmp_path.resolve() / ".awm"
