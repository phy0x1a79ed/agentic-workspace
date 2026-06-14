"""Tests for the `dev` service: manifest/handler shape, lifecycle gating, and
the adapter self-shadow gate.

These run without a live hub — the adapter's per-target loop is mocked, and
``run_lifecycle`` shells through a mocked ``subprocess.run``. The point is to
pin the two behaviours that make the dev service safe:

1. lifecycle is INERT on a prod base (no ``AWM_SHADOW_HUB_URL``) — it must not
   start/stop anything when it isn't a dev sandbox's dev service;
2. the adapter opens a self-shadow overlay loop iff a shadow hub url is given
   AND differs from the primary — never otherwise (so feature services that
   call ``run()`` with no shadow target never self-shadow).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from awm.dev import dev
from awm.dev import hub_adapter
from awm.gatewayclient import ServiceAdapter


# -- manifest / handler shape -------------------------------------------------

def test_manifest_tools_are_dev_prefixed():
    tools = [f["tool"] for f in hub_adapter.API_MANIFEST["functions"]]
    assert tools == ["dev_start", "dev_stop", "dev_restart", "dev_seed", "dev_status"]


def test_handlers_cover_every_manifest_function():
    fns = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
    assert set(hub_adapter.HANDLERS) == fns


# -- run_lifecycle gating -----------------------------------------------------

def test_lifecycle_inert_on_prod_base(monkeypatch):
    """No AWM_SHADOW_HUB_URL → a prod base → handlers return an inert marker and
    never touch run.sh (the CLI falls back to a local run)."""
    monkeypatch.setattr(dev, "IS_DEV_SANDBOX", False)

    def _boom(*a, **k):  # run.sh must NOT be invoked on a prod base
        raise AssertionError("subprocess.run called on a prod base")

    monkeypatch.setattr(subprocess, "run", _boom)
    out = dev.run_lifecycle("status")
    assert out["inert"] is True
    assert "action" in out and out["action"] == "status"


def test_lifecycle_acts_on_dev_sandbox(monkeypatch):
    """AWM_SHADOW_HUB_URL set → a dev sandbox → handlers shell to run.sh with the
    requested action and surface rc/stdout/stderr."""
    monkeypatch.setattr(dev, "IS_DEV_SANDBOX", True)
    calls = {}

    def _fake_run(argv, **kw):
        calls["argv"] = argv
        calls["cwd"] = kw.get("cwd")
        return SimpleNamespace(returncode=0, stdout="[dev] ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = dev.run_lifecycle("restart")
    assert out == {"action": "restart", "rc": 0, "stdout": "[dev] ok\n", "stderr": ""}
    assert calls["argv"][0] == "bash"
    assert calls["argv"][1].endswith("awm/gateway/dev/run.sh")
    assert calls["argv"][2] == "restart"


def test_lifecycle_rejects_unknown_action(monkeypatch):
    monkeypatch.setattr(dev, "IS_DEV_SANDBOX", True)
    out = dev.run_lifecycle("nuke")
    assert out["rc"] == 2 and "unknown" in out["error"]


# -- adapter self-shadow gate -------------------------------------------------

def _adapter_with_recorder(monkeypatch):
    """A ServiceAdapter whose per-target loop is replaced by a recorder, so
    run() does no real networking. Returns (adapter, recorded_targets)."""
    adapter = ServiceAdapter("dev", {"functions": []}, {})
    targets: list[dict] = []

    async def _record(hub_url, sid, *, name, prefix, overlay):
        targets.append({"hub_url": hub_url, "name": name,
                        "prefix": prefix, "overlay": overlay})

    monkeypatch.setattr(adapter, "_run_target", _record)
    return adapter, targets


@pytest.mark.asyncio
async def test_no_self_shadow_without_target(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7821/")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)
    monkeypatch.delenv("AWM_SERVICE_OVERLAY", raising=False)
    monkeypatch.delenv("AWM_SERVICE_PREFIX", raising=False)
    adapter, targets = _adapter_with_recorder(monkeypatch)

    await adapter.run(shadow_hub_url=None)
    assert len(targets) == 1
    assert targets[0]["overlay"] is False
    assert targets[0]["prefix"] == "/svc/dev"


@pytest.mark.asyncio
async def test_self_shadow_opens_overlay_loop(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7821/")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)
    monkeypatch.delenv("AWM_SERVICE_OVERLAY", raising=False)
    monkeypatch.delenv("AWM_SERVICE_PREFIX", raising=False)
    adapter, targets = _adapter_with_recorder(monkeypatch)

    await adapter.run(shadow_hub_url="http://127.0.0.1:7819/")
    assert len(targets) == 2
    base, overlay = targets
    assert base["hub_url"] == "http://127.0.0.1:7821" and base["overlay"] is False
    assert overlay["hub_url"] == "http://127.0.0.1:7819"
    assert overlay["name"] == "dev-shadow"
    assert overlay["prefix"] == "/svc/dev"
    assert overlay["overlay"] is True


@pytest.mark.asyncio
async def test_self_shadow_skipped_when_equal_to_primary(monkeypatch):
    """A misconfigured sandbox that shadows its own hub must not double-register."""
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819/")
    monkeypatch.delenv("AWM_SERVICE_ID", raising=False)
    monkeypatch.delenv("AWM_SERVICE_OVERLAY", raising=False)
    monkeypatch.delenv("AWM_SERVICE_PREFIX", raising=False)
    adapter, targets = _adapter_with_recorder(monkeypatch)

    await adapter.run(shadow_hub_url="http://127.0.0.1:7819/")
    assert len(targets) == 1
