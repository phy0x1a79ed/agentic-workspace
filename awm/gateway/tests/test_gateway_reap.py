"""``services reap`` backstop (cleanup plan T2).

The reaper finds ``python -m awm.<svc>.hub_adapter`` orphans that target THIS
gateway's origin but hold no live registry lease, and SIGTERM→SIGKILL them. It
must:

* match only on ``AWM_HUB_URL`` reduced to ``host:port`` — so a prod reap
  (``:7819``) never touches the dev hub's children (``:7821``);
* never reap a pid that holds a live lease (a healthy registered base);
* never reap the gateway's own pid;
* honour ``--dry-run`` (list, don't kill);
* reuse the supervisor's ``kill_pid_group`` for escalation.

These drive ``_op_services_reap`` directly against the real registry/lease
singletons (rebound per-test) with ``_scan_hub_adapters`` + ``kill_pid_group``
faked, so no real processes are touched.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.hub]

from awm.gateway import gateway_ops as go
from awm.gateway.hub import lease as lease_mod
from awm.gateway.hub import registry as reg_mod
from awm.gateway.hub import supervisor as sup_mod


@pytest.fixture(autouse=True)
def _isolate(awm_workspace):
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None  # rebound to the fresh registry on next get
    yield
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None


def _hold_lease(service_id: str) -> None:
    lease_mod.get_lease_manager()._holders[service_id] = asyncio.Event()


async def _register_base(name: str, pid: int):
    return await reg_mod.get_registry().register_service(
        name, f"/svc/{name}", pid=pid, start_cmd=["bash", "run.sh"], cwd="/x")


def _fake_scan(monkeypatch, procs):
    """Fake the /proc scan. ``age_s`` defaults to well past any grace — a
    process too young to have registered is spared, and that is its own test."""
    filled = [{"age_s": 3600.0, **p} for p in procs]
    monkeypatch.setattr(go, "_scan_hub_adapters", lambda: list(filled))


def _capture_kills(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(sup_mod, "kill_pid_group", lambda pid, **k: killed.append(pid))
    # default_hub_url drives the origin filter; pin it deterministically.
    monkeypatch.setattr(sup_mod, "default_hub_url", lambda: "http://127.0.0.1:7819/")
    return killed


# ---------------------------------------------------------------------------
# _origin_of normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:7819/", "127.0.0.1:7819"),
    ("http://127.0.0.1:7819", "127.0.0.1:7819"),
    ("ws://127.0.0.1:7821/x", "127.0.0.1:7821"),
    ("127.0.0.1:7819", "127.0.0.1:7819"),
    ("", ""),
    (None, ""),
])
def test_origin_of(url, expected):
    assert go._origin_of(url) == expected


# ---------------------------------------------------------------------------
# reap filtering
# ---------------------------------------------------------------------------


async def test_reap_kills_only_matching_origin_orphans(monkeypatch):
    killed = _capture_kills(monkeypatch)
    _fake_scan(monkeypatch, [
        {"pid": 1001, "cmdline": "python -m awm.agents.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},   # prod orphan → reap
        {"pid": 1002, "cmdline": "python -m awm.tts.hub_adapter",
         "hub_url": "http://127.0.0.1:7821/"},   # dev hub child → keep
        {"pid": 1003, "cmdline": "python -m awm.skills.hub_adapter",
         "hub_url": ""},                          # no hub url → keep
    ])
    out = await go._op_services_reap(go.ReapRequest(dry_run=False))
    assert out["origin"] == "127.0.0.1:7819"
    assert killed == [1001]
    assert [p["pid"] for p in out["reaped"]] == [1001]
    assert out["count"] == 1


async def test_reap_skips_live_lease_holders(monkeypatch):
    killed = _capture_kills(monkeypatch)
    # A healthy base on this origin holds a lease — must NOT be reaped.
    rec = await _register_base("agents", pid=2001)
    _hold_lease(rec.service_id)
    _fake_scan(monkeypatch, [
        {"pid": 2001, "cmdline": "python -m awm.agents.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},   # live base → keep
        {"pid": 2002, "cmdline": "python -m awm.agents.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},   # extra generation → reap
    ])
    out = await go._op_services_reap(go.ReapRequest(dry_run=False))
    assert killed == [2002]
    assert out["count"] == 1


async def test_reap_never_kills_own_pid(monkeypatch):
    import os
    killed = _capture_kills(monkeypatch)
    _fake_scan(monkeypatch, [
        {"pid": os.getpid(), "cmdline": "python -m awm.gateway.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])
    out = await go._op_services_reap(go.ReapRequest(dry_run=False))
    assert killed == []
    assert out["count"] == 0


async def test_reap_dry_run_lists_without_killing(monkeypatch):
    killed = _capture_kills(monkeypatch)
    _fake_scan(monkeypatch, [
        {"pid": 3001, "cmdline": "python -m awm.social.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])
    out = await go._op_services_reap(go.ReapRequest(dry_run=True))
    assert killed == []                 # nothing killed
    assert out["dry_run"] is True
    assert out["reaped"] == []          # reaped list empty on dry-run
    assert [p["pid"] for p in out["found"]] == [3001]  # but found + listed
    assert out["count"] == 1


# ---------------------------------------------------------------------------
# Operation projects onto all three surfaces
# ---------------------------------------------------------------------------


def test_reap_operation_registered_on_all_surfaces():
    op = next(o for o in go.GATEWAY_OPERATIONS if o.name == "services_reap")
    assert op.surfaces == frozenset({"cli", "mcp", "http"})
    assert op.cli_group == "services" and op.cli_command == "reap"
    assert op.http_method == "POST" and op.http_path == "/hub/services/reap"
    assert op.request_model is go.ReapRequest


def test_reap_projects_to_mcp_tool():
    from awm.gateway.operations import operations_to_mcp_tools

    tools = {t.name: t for t in operations_to_mcp_tools(go.GATEWAY_OPERATIONS)}
    assert "services_reap" in tools
    props = tools["services_reap"].inputSchema["properties"]
    assert props["dry_run"]["type"] == "boolean"


# ---------------------------------------------------------------------------
# Authority over zombies: a lease is possession, not health.
#
# The 2026-07-27 outage survived a targeted cleanup because stale instances
# kept their slot without ever serving — every replacement was refused in
# favour of a corpse. So a holder that never reached ready, past a grace, is
# reapable; a ready one, an overlay, and a still-starting one are not.
# ---------------------------------------------------------------------------


def _claim_lease(service_id: str, *, held_for: float = 0.0) -> None:
    """Take the lease the way the control-WS handler does, optionally
    backdating when it was taken."""
    import time
    lm = lease_mod.get_lease_manager()
    lm._holders[service_id] = asyncio.Event()
    lm._claimed_at[service_id] = time.monotonic() - held_for


def _mark_ready(service_id: str) -> None:
    from awm.gateway.hub import rpc as rpc_mod
    rpc_mod.ensure_control(service_id).set_api({})


@pytest.fixture(autouse=True)
def _clean_rpc():
    from awm.gateway.hub import rpc as rpc_mod
    rpc_mod._channels.clear()
    yield
    rpc_mod._channels.clear()


async def test_unready_lease_holder_past_grace_is_reaped(monkeypatch):
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    rec = await _register_base("zombie", pid=4001)
    _claim_lease(rec.service_id, held_for=120.0)     # holding, never ready
    _fake_scan(monkeypatch, [
        {"pid": 4001, "cmdline": "python -m awm.zombie.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])

    out = await go.reap_orphans()

    assert killed == [4001]
    assert out["count"] == 1


async def test_a_ready_lease_holder_is_never_reaped(monkeypatch):
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    rec = await _register_base("healthy", pid=4002)
    _claim_lease(rec.service_id, held_for=99999.0)   # ancient, but serving
    _mark_ready(rec.service_id)
    _fake_scan(monkeypatch, [
        {"pid": 4002, "cmdline": "python -m awm.healthy.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])

    out = await go.reap_orphans()

    assert killed == []
    assert out["count"] == 0


async def test_a_still_starting_holder_is_spared(monkeypatch):
    """Inside the grace, an unready holder is a slow starter, not a corpse."""
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    rec = await _register_base("booting", pid=4003)
    _claim_lease(rec.service_id, held_for=1.0)
    _fake_scan(monkeypatch, [
        {"pid": 4003, "cmdline": "python -m awm.booting.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])

    out = await go.reap_orphans()

    assert killed == []
    assert out["count"] == 0


async def test_an_overlay_is_never_reaped(monkeypatch):
    """`awm dev shadow` owns its own lifecycle — it is never journaled, never
    respawn-watchdogged, and must never be reaped either."""
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    rec = await _register_base("shadowed", pid=4004)
    rec.is_overlay = True
    _claim_lease(rec.service_id, held_for=99999.0)   # unready and ancient
    _fake_scan(monkeypatch, [
        {"pid": 4004, "cmdline": "python -m awm.shadowed.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/"},
    ])

    out = await go.reap_orphans()

    assert killed == []
    assert out["count"] == 0


async def test_origin_check_precedes_the_zombie_rule(monkeypatch):
    """A wedged dev-sandbox child must survive a prod sweep. The origin filter
    is the only thing standing between the two, so it cannot be conditional on
    the health verdict."""
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    _fake_scan(monkeypatch, [
        {"pid": 4005, "cmdline": "python -m awm.notes.hub_adapter",
         "hub_url": "http://127.0.0.1:7821/"},       # dev sandbox, unregistered
    ])

    out = await go.reap_orphans()

    assert killed == []
    assert out["count"] == 0


async def test_a_freshly_spawned_process_is_spared(monkeypatch):
    """The sweep is automatic now, so it will regularly run while a service is
    mid-registration: no lease yet, no record yet, indistinguishable from an
    orphan by every other signal."""
    killed = _capture_kills(monkeypatch)
    monkeypatch.setattr(go, "_READY_GRACE_S", 30.0)
    _fake_scan(monkeypatch, [
        {"pid": 5001, "cmdline": "python -m awm.notes.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/", "age_s": 2.0},
    ])

    out = await go.reap_orphans()

    assert killed == []
    assert out["count"] == 0


async def test_an_unknowable_age_is_treated_as_young(monkeypatch):
    """/proc read raced the exit, or this is not Linux. Never guess toward
    killing."""
    killed = _capture_kills(monkeypatch)
    _fake_scan(monkeypatch, [
        {"pid": 5002, "cmdline": "python -m awm.notes.hub_adapter",
         "hub_url": "http://127.0.0.1:7819/", "age_s": None},
    ])

    out = await go.reap_orphans()

    assert killed == []


def test_proc_age_of_this_process_is_plausible():
    """The /proc/<pid>/stat field-22 arithmetic, against a real process."""
    import os as _os
    age = go._proc_age_s(_os.getpid())
    assert age is not None
    assert 0.0 <= age < 86400.0


def test_proc_age_of_a_dead_pid_is_none():
    assert go._proc_age_s(2 ** 22) is None
