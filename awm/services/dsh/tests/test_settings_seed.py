"""Seeding the model route: add it once, then never touch the file again.

``settings.yaml`` belongs to the user and to the GUI, which both write it. Every
test here is really one claim — that this service is a first-run convenience and
not a config manager that reasserts itself on every deploy.
"""

from __future__ import annotations

import importlib

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def mod(tmp_path, monkeypatch):
    """Reload the modules against a throwaway DSH_HOME.

    The paths are module constants resolved at import, which is right for a
    service with one state directory and wrong for a test — so the environment
    is set and the modules re-imported, rather than the constants patched one by
    one and silently left inconsistent.
    """
    monkeypatch.setenv("DSH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "home"))
    harness = importlib.reload(importlib.import_module("awm.dsh.harness"))
    settings = importlib.reload(importlib.import_module("awm.dsh.settings"))
    yield settings
    # Leave the modules bound to the real environment for anything after us.
    monkeypatch.undo()
    importlib.reload(harness)
    importlib.reload(settings)


def _read(settings):
    return yaml.safe_load(settings.SETTINGS_FILE.read_text())


def test_a_missing_settings_file_gets_the_provider(mod):
    assert mod.ensure()["changed"] is True
    providers = _read(mod)[mod.PLUGIN_KEY]["providers"]
    assert providers["openrouter"]["baseURL"] == mod.BASE_URL
    assert providers["openrouter"]["api"] == "openai-completions"


def test_seeding_is_idempotent(mod):
    mod.ensure()
    before = mod.SETTINGS_FILE.read_text()
    assert mod.ensure()["changed"] is False
    assert mod.SETTINGS_FILE.read_text() == before


def test_an_existing_provider_is_left_exactly_as_the_user_left_it(mod):
    """A route tuned in the GUI must survive a restart and a deploy."""
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mine = {mod.PLUGIN_KEY: {"providers": {"openrouter": {"models": [{"id": "mine"}]}}}}
    mod.SETTINGS_FILE.write_text(yaml.safe_dump(mine))
    assert mod.ensure()["changed"] is False
    assert _read(mod) == mine


def test_unrelated_settings_survive_the_seed(mod):
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mod.SETTINGS_FILE.write_text(yaml.safe_dump({"theme": "dark"}))
    mod.ensure()
    assert _read(mod)["theme"] == "dark"


def test_an_unparseable_file_is_reported_not_rewritten(mod):
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mod.SETTINGS_FILE.write_text("this: [is: not: yaml\n")
    before = mod.SETTINGS_FILE.read_text()
    res = mod.ensure()
    assert res["changed"] is False and res["error"]
    assert mod.SETTINGS_FILE.read_text() == before


def test_the_credential_is_referenced_by_env_name_never_written(mod):
    """The one thing about this file that is a security property rather than a
    convenience: it is plain text in a state directory."""
    mod.ensure()
    text = mod.SETTINGS_FILE.read_text()
    assert "apiKeyEnv: OPENROUTER_API_KEY" in text
    assert "sk-" not in text
    assert "apiKey:" not in text
