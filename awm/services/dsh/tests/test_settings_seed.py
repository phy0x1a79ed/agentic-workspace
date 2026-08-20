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
    mine = {"providers": {"openrouter": {"models": [{"id": "mine"}]}}}
    mod.SETTINGS_FILE.write_text(yaml.safe_dump({mod.PLUGIN_KEY: mine}))
    mod.ensure()
    assert _read(mod)[mod.PLUGIN_KEY] == mine


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


def test_the_default_model_selection_is_seeded_too(mod):
    """A declared route the default selection does not point at is a working
    provider list and a dead harness: the profile ships a DeepSeek-direct
    default whose credential this workspace does not hold."""
    mod.ensure()
    sel = _read(mod)[mod.DEFAULT_MODEL_KEY]
    assert sel == {"provider": "openrouter", "model": mod.MODELS[0]}


def test_a_chosen_default_model_is_not_overwritten(mod):
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mine = {mod.DEFAULT_MODEL_KEY: {"provider": "openrouter", "model": "mine"}}
    mod.SETTINGS_FILE.write_text(yaml.safe_dump(mine))
    mod.ensure()
    assert _read(mod)[mod.DEFAULT_MODEL_KEY]["model"] == "mine"


def test_the_two_sections_are_seeded_independently(mod):
    """Someone who deleted only the provider block gets only that back."""
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mod.SETTINGS_FILE.write_text(yaml.safe_dump(
        {mod.DEFAULT_MODEL_KEY: {"provider": "openrouter", "model": "mine"}}))
    assert mod.ensure()["seeded"] == ["provider"]


# ---------------------------------------------------------------------------
# Steering the route from awm, because the harness's Settings UI is loopback-only
# ---------------------------------------------------------------------------

def test_selecting_a_declared_model_moves_the_default(mod):
    mod.ensure()
    res = mod.set_default_model(mod.MODELS[2])
    assert res["changed"] is True and res["previous"] == mod.MODELS[0]
    assert _read(mod)[mod.DEFAULT_MODEL_KEY]["model"] == mod.MODELS[2]


def test_selecting_the_same_model_twice_reports_no_change(mod):
    mod.ensure()
    assert mod.set_default_model(mod.MODELS[0])["changed"] is False


def test_an_undeclared_model_is_refused_unless_declared(mod):
    """A selection naming a model the route does not advertise is the exact
    failure this verb exists to prevent."""
    mod.ensure()
    with pytest.raises(ValueError, match="not declared"):
        mod.set_default_model("vendor/not-listed")
    assert _read(mod)[mod.DEFAULT_MODEL_KEY]["model"] == mod.MODELS[0]

    res = mod.set_default_model("vendor/not-listed", declare=True)
    assert res["model"] == "vendor/not-listed"
    assert "vendor/not-listed" in mod.declared_models()


def test_a_write_never_lands_while_the_harness_holds_the_lock(mod):
    """Stealing the lock would turn a delay into a lost write, so we time out."""
    mod.ensure()
    lock = mod.SETTINGS_FILE.with_name(mod.SETTINGS_FILE.name + ".lock")
    lock.write_text("")
    try:
        mod.LOCK_TIMEOUT_S = 0.05
        with pytest.raises(mod.SettingsUnavailable, match="is held"):
            mod.set_default_model(mod.MODELS[1])
        assert lock.exists(), "the contender must not remove somebody else's lock"
    finally:
        lock.unlink()
    assert _read(mod)[mod.DEFAULT_MODEL_KEY]["model"] == mod.MODELS[0]


def test_an_unparseable_document_is_never_partially_rewritten(mod):
    mod.DSH_HOME.mkdir(parents=True, exist_ok=True)
    mod.SETTINGS_FILE.write_text("this: [is: not: yaml\n")
    before = mod.SETTINGS_FILE.read_text()
    with pytest.raises(mod.SettingsUnavailable, match="does not parse"):
        mod.set_default_model("anything", declare=True)
    assert mod.SETTINGS_FILE.read_text() == before


def test_unrelated_sections_survive_a_model_change(mod):
    mod.ensure()
    doc = _read(mod)
    doc["some-other-plugin"] = {"kept": True}
    mod.SETTINGS_FILE.write_text(yaml.safe_dump(doc))
    mod.set_default_model(mod.MODELS[1])
    assert _read(mod)["some-other-plugin"] == {"kept": True}
