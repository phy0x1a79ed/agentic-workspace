"""Device-discovery tests for config.py — filesystem layout -> {name: DeviceCreds}."""

from __future__ import annotations

import pytest

from awm.twofa import config as cfg


def _write_pair(d, creds_name, key_name):
    (d / creds_name).write_text("{}")
    (d / key_name).write_text("x")


@pytest.mark.smoke
def test_paths_for_legacy_and_named(tmp_path):
    legacy = cfg.paths_for("cwl", tmp_path)
    assert legacy.creds_path == tmp_path / "creds.json"
    assert legacy.key_path == tmp_path / "device_key.pem"

    named = cfg.paths_for("alliance", tmp_path)
    assert named.creds_path == tmp_path / "alliance-creds.json"
    assert named.key_path == tmp_path / "alliance-key.pem"


@pytest.mark.smoke
def test_discover_legacy_only(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_2FA_CREDS_PATH", raising=False)
    monkeypatch.delenv("AWM_2FA_KEY_PATH", raising=False)
    _write_pair(tmp_path, "creds.json", "device_key.pem")
    devices = cfg.discover_devices(tmp_path)
    assert set(devices) == {"cwl"}
    assert devices["cwl"].enrolled is True


@pytest.mark.smoke
def test_discover_legacy_plus_named(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_2FA_CREDS_PATH", raising=False)
    monkeypatch.delenv("AWM_2FA_KEY_PATH", raising=False)
    _write_pair(tmp_path, "creds.json", "device_key.pem")
    _write_pair(tmp_path, "alliance-creds.json", "alliance-key.pem")
    devices = cfg.discover_devices(tmp_path)
    assert set(devices) == {"cwl", "alliance"}
    assert devices["cwl"].enrolled and devices["alliance"].enrolled


@pytest.mark.smoke
def test_discover_glob_does_not_match_bare_creds(tmp_path, monkeypatch):
    # "*-creds.json" must not pick up the legacy "creds.json".
    monkeypatch.delenv("AWM_2FA_CREDS_PATH", raising=False)
    monkeypatch.delenv("AWM_2FA_KEY_PATH", raising=False)
    _write_pair(tmp_path, "creds.json", "device_key.pem")
    devices = cfg.discover_devices(tmp_path)
    # Only the explicit legacy branch creates cwl; no "" device from the glob.
    assert set(devices) == {"cwl"}
    assert "" not in devices


@pytest.mark.smoke
def test_discover_named_without_key_is_unenrolled(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_2FA_CREDS_PATH", raising=False)
    monkeypatch.delenv("AWM_2FA_KEY_PATH", raising=False)
    (tmp_path / "alliance-creds.json").write_text("{}")  # no sibling key
    devices = cfg.discover_devices(tmp_path)
    assert "alliance" in devices
    assert devices["alliance"].enrolled is False


@pytest.mark.smoke
def test_discover_empty_dir_lists_cwl_unenrolled(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_2FA_CREDS_PATH", raising=False)
    monkeypatch.delenv("AWM_2FA_KEY_PATH", raising=False)
    devices = cfg.discover_devices(tmp_path)
    assert set(devices) == {"cwl"}
    assert devices["cwl"].enrolled is False


@pytest.mark.smoke
def test_env_override_forces_single_device(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_2FA_CREDS_PATH", str(tmp_path / "x.json"))
    monkeypatch.setenv("AWM_2FA_KEY_PATH", str(tmp_path / "x.pem"))
    monkeypatch.setenv("AWM_2FA_DEVICE_NAME", "mydev")
    # Even with other files present, the env override wins and yields one device.
    _write_pair(tmp_path, "alliance-creds.json", "alliance-key.pem")
    devices = cfg.discover_devices(tmp_path)
    assert set(devices) == {"mydev"}
    assert devices["mydev"].creds_path == tmp_path / "x.json"


@pytest.mark.smoke
def test_env_override_default_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_2FA_CREDS_PATH", str(tmp_path / "x.json"))
    monkeypatch.setenv("AWM_2FA_KEY_PATH", str(tmp_path / "x.pem"))
    monkeypatch.delenv("AWM_2FA_DEVICE_NAME", raising=False)
    devices = cfg.discover_devices(tmp_path)
    assert set(devices) == {"cwl"}
