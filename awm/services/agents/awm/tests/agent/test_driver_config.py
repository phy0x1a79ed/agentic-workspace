"""The agents default-driver config contract — the first real ConfigContract.

Covers the service-side half: the opt-in manifest wiring (schema published,
config_get/config_set as RPC handlers but NOT projected tools), the
load/save/validate roundtrip against the service's own DB, and the placement
default resolution reading the stored value between the env override and the
historical literal.
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


def test_defaults_then_persist(driver_db):
    assert driver_db.load() == {"harness": "opencode", "model": None}
    saved = driver_db.save({"harness": "claude", "model": "haiku"})
    assert saved == {"harness": "claude", "model": "haiku"}
    assert driver_db.load() == {"harness": "claude", "model": "haiku"}


def test_partial_patch_merges_over_stored(driver_db):
    driver_db.save({"harness": "claude", "model": "haiku"})
    # A patch touching only one field leaves the other stored value intact.
    assert driver_db.save({"model": "opus"}) == {"harness": "claude", "model": "opus"}


def test_validation_rejects_bad_harness(driver_db):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        driver_db.save({"harness": "bogus"})
    # A rejected write never reaches the DB.
    assert driver_db.load() == {"harness": "opencode", "model": None}


def test_config_get_set_handlers(driver_db):
    got = driver_db.get()
    assert got["title"] == "Agent default driver"
    assert got["values"] == {"harness": "opencode", "model": None}
    assert "harness" in got["schema"]["properties"]
    out = driver_db.set({"values": {"harness": "claude"}})
    assert out["values"]["harness"] == "claude"
    assert driver_db.get()["values"]["harness"] == "claude"


def test_placement_default_resolution(driver_db, monkeypatch):
    """The stored contract sits between env and the historical literal; explicit
    args and env still win (preserving every current call site)."""
    from awm.agents import placement

    monkeypatch.delenv("AWM_PLACEMENT_HARNESS", raising=False)
    monkeypatch.delenv("AWM_PLACEMENT_MODEL", raising=False)

    # Untouched contract → historical default.
    assert placement._resolve_driver({}) == ("opencode", None)

    # A saved value beats the literal.
    driver_db.save({"harness": "claude", "model": "haiku"})
    assert placement._resolve_driver({}) == ("claude", "haiku")

    # Explicit args beat the contract.
    assert placement._resolve_driver({"harness": "opencode"}) == ("opencode", "haiku")

    # Env beats the contract too.
    monkeypatch.setenv("AWM_PLACEMENT_HARNESS", "opencode")
    assert placement._resolve_driver({})[0] == "opencode"
