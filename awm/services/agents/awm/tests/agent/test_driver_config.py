"""The agents default-driver config contract — the first real ConfigContract.

Covers the service-side half: the opt-in manifest wiring (schema published,
config_get/config_set as RPC handlers but NOT projected tools), the
load/save/validate roundtrip against the service's own DB, and the placement
default resolution reading the stored value between the env override and the
field default (the uniform claude/haiku/medium — T5).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.unit]


@pytest.fixture
def driver_db(tmp_path, monkeypatch):
    """Point the per-service DB at tmp so the contract reads/writes in isolation."""
    import awm.persistence.databases as dbs

    monkeypatch.setattr(dbs, "SERVICES_DIR", tmp_path / "services")
    from awm.agents.driver_config import DRIVER_CONTRACT

    return DRIVER_CONTRACT


def test_manifest_opt_in_and_handlers():
    """The agents service opts in: a `config` block + RPC handlers, but the
    config verbs are deliberately absent from the projected functions list."""
    from awm.agents import hub_adapter as hub

    cfg = hub.API_MANIFEST.get("config")
    assert cfg is not None
    assert cfg["title"] == "Agent default driver"
    assert "harness" in cfg["schema"]["properties"]
    assert "config_get" in hub.HANDLERS and "config_set" in hub.HANDLERS
    fn_names = {f["name"] for f in hub.API_MANIFEST["functions"]}
    assert "config_get" not in fn_names and "config_set" not in fn_names


# The UNIFORM placement default (T5): every placement — attended or detached —
# defaults to the same driver. The old opencode/None default is gone.
_DEFAULT = {"harness": "claude", "model": "haiku", "effort": "medium"}


def test_defaults_then_persist(driver_db):
    assert driver_db.load() == _DEFAULT
    saved = driver_db.save({"harness": "opencode", "model": None})
    assert saved == {"harness": "opencode", "model": None, "effort": "medium"}
    assert driver_db.load() == {"harness": "opencode", "model": None,
                                "effort": "medium"}


def test_partial_patch_merges_over_stored(driver_db):
    driver_db.save({"harness": "claude", "model": "haiku"})
    # A patch touching only one field leaves the other stored values intact.
    assert driver_db.save({"model": "opus"}) == {
        "harness": "claude", "model": "opus", "effort": "medium"}


def test_validation_rejects_bad_harness(driver_db):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        driver_db.save({"harness": "bogus"})
    # A rejected write never reaches the DB.
    assert driver_db.load() == _DEFAULT


def test_config_get_set_handlers(driver_db):
    got = driver_db.get()
    assert got["title"] == "Agent default driver"
    assert got["values"] == _DEFAULT
    assert "harness" in got["schema"]["properties"]
    assert "effort" in got["schema"]["properties"]
    out = driver_db.set({"values": {"harness": "opencode"}})
    assert out["values"]["harness"] == "opencode"
    assert driver_db.get()["values"]["harness"] == "opencode"


def test_placement_default_resolution(driver_db, monkeypatch):
    """The stored contract sits between env and the field default; explicit args
    and env still win (preserving every current call site). Returns
    ``(harness, model, effort)`` — the uniform claude/haiku/medium by default."""
    from awm.agents import placement

    monkeypatch.delenv("AWM_PLACEMENT_HARNESS", raising=False)
    monkeypatch.delenv("AWM_PLACEMENT_MODEL", raising=False)
    monkeypatch.delenv("AWM_PLACEMENT_EFFORT", raising=False)

    # Untouched contract → the uniform default (no attach-state fork).
    assert placement._resolve_driver({}) == ("claude", "haiku", "medium")

    # A saved value beats the field default (opencode stays available). A
    # blank model is never left blank: it resolves to the harness's EXPLICIT
    # model default (blank no longer resolves to None / an ambient-inherited
    # harness default).
    driver_db.save({"harness": "opencode", "model": None})
    assert placement._resolve_driver({}) == (
        "opencode", placement.DEFAULT_OPENCODE_PLACEMENT_MODEL, "medium")

    # Explicit args beat the contract.
    assert placement._resolve_driver({"harness": "claude", "model": "opus"}) == (
        "claude", "opus", "medium")

    # Env beats the contract too.
    monkeypatch.setenv("AWM_PLACEMENT_HARNESS", "claude")
    monkeypatch.setenv("AWM_PLACEMENT_EFFORT", "high")
    assert placement._resolve_driver({})[0] == "claude"
    assert placement._resolve_driver({})[2] == "high"
