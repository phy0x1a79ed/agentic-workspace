"""The service surface: manifest shape and handler dispatch.

The manifest is what the gateway compiles into MCP, CLI and HTTP surfaces, so
its mistakes show up as a broken CLI at import time rather than as a test
failure. Two of them have bitten this tree before and are asserted here:
optional parameters ahead of required ones (which once crashed the generated
CLI), and projected tool names that do not fold cleanly onto a single domain.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.smoke


@pytest.fixture(scope="module", autouse=True)
def isolated_state():
    """Never let a test write to the production service DB."""
    tmp = tempfile.mkdtemp(prefix="compute-test-")
    saved = {k: os.environ.get(k) for k in
             ("AWM_WORKSPACE", "AWM_COMPUTE_NOTICE_DIR", "AWM_HUB_URL")}
    os.environ["AWM_WORKSPACE"] = tmp
    os.environ["AWM_COMPUTE_NOTICE_DIR"] = os.path.join(tmp, "notices")
    os.environ["AWM_HUB_URL"] = "http://127.0.0.1:7899"   # not prod: cannot arm
    yield tmp
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def adapter(isolated_state):
    from awm.compute import hub_adapter
    hub_adapter.WATCHER.setup()
    return hub_adapter


def test_manifest_is_serializable_and_folds_onto_one_domain(adapter):
    json.dumps(adapter.API_MANIFEST)
    for fn in adapter.API_MANIFEST["functions"]:
        domain, _, verb = fn["tool"].partition("_")
        assert domain == "compute", fn["tool"]
        assert verb, f"{fn['tool']} would become its own junk single-verb domain"


def test_every_declared_function_has_a_handler(adapter):
    declared = {f["name"] for f in adapter.API_MANIFEST["functions"]}
    assert declared == set(adapter.HANDLERS)


def test_required_params_come_before_optional_ones(adapter):
    """Required-after-optional in a manifest once crashed the generated CLI at
    import time — a failure that lands nowhere near the service that caused it."""
    for fn in adapter.API_MANIFEST["functions"]:
        seen_optional = False
        for p in fn["params"]:
            if p.get("required"):
                assert not seen_optional, f"{fn['name']}: {p['name']} after an optional"
            else:
                seen_optional = True


def test_the_escape_hatch_is_off_the_mcp_surface(adapter):
    """`grant` needs the caller's own shell to expand its session id; over MCP
    there is no shell and no honest way to know who is asking."""
    by_name = {f["name"]: f for f in adapter.API_MANIFEST["functions"]}
    assert by_name["grant"]["surfaces"] == ["cli", "http"]
    assert by_name["revoke"]["surfaces"] == ["cli", "http"]
    assert "surfaces" not in by_name["status"]


def test_status_reports_derived_caps_and_the_arming_guard(adapter):
    s = adapter.HANDLERS["status"]({})
    assert s["box"]["cores"] > 0
    assert s["caps"]["hard_cpu_cores"] == s["box"]["cores"] - 2
    assert s["arm_eligible"] is False   # not the production origin
    assert s["armed"] is False
    assert "duty_cycle_pct_of_one_core" in s["duty"]


def test_arm_cannot_go_live_on_a_non_production_origin(adapter):
    out = adapter.HANDLERS["arm"]({"mode": "live"})
    assert out["mode"] == "observe"
    assert out["armed"] is False
    assert "not the production origin" in out["note"]


def test_arm_rejects_an_unknown_mode(adapter):
    with pytest.raises(ValueError, match="unknown mode"):
        adapter.HANDLERS["arm"]({"mode": "yolo"})


def test_arm_reads_the_current_posture_without_changing_it(adapter):
    before = adapter.HANDLERS["arm"]({})
    assert before["requested"] is None
    assert adapter.HANDLERS["arm"]({})["mode"] == before["mode"]


def test_every_posture_is_reachable_from_the_cli(adapter):
    """The rollback direction is the one that has to work under pressure.

    A boolean manifest parameter becomes a bare CLI flag, which can only turn a
    setting on — so postures are a single string, and going back is just
    another value.
    """
    for mode in ("observe", "shadow", "live"):
        params = adapter.API_MANIFEST["functions"]
        arm = next(f for f in params if f["name"] == "arm")
        assert arm["params"][0]["type"] == "string"
        assert mode in adapter.MODES


def test_tune_rejects_unknown_thresholds_and_applies_known_ones(adapter):
    with pytest.raises(ValueError, match="unknown thresholds"):
        adapter.HANDLERS["tune"]({"values": {"nonsense": 1}})

    adapter.HANDLERS["tune"]({"values": {"mem_reserve_gb": 12.0}})
    assert adapter.HANDLERS["tune"]({})["thresholds"]["mem_reserve_gb"] == 12.0
    adapter.HANDLERS["tune"]({"reset": True})
    assert adapter.HANDLERS["tune"]({})["thresholds"]["mem_reserve_gb"] == 8.0


def test_grants_are_bounded_in_size_time_and_reason(adapter):
    with pytest.raises(ValueError):
        adapter.HANDLERS["grant"]({"session": "s", "reason": "x"})  # no size
    with pytest.raises(KeyError):
        adapter.HANDLERS["grant"]({"session": "s", "mem_gb": 4})    # no reason

    out = adapter.HANDLERS["grant"](
        {"session": "s", "reason": "big embedding run", "mem_gb": 40,
         "ttl_min": 10_000})
    ttl = out["grant"]["expires_at"] - out["grant"]["created_at"]
    assert ttl == pytest.approx(3600, abs=1), "a grant may not be open-ended"
    assert adapter.HANDLERS["grants"]({})["grants"]
    assert adapter.HANDLERS["revoke"]({"session": "s"})["revoked"] == 1
    assert not adapter.HANDLERS["grants"]({})["grants"]


def test_explain_answers_for_our_own_pid(adapter):
    out = adapter.HANDLERS["explain"]({"pid": os.getpid()})
    assert out["found"]
    assert out["ancestors"]
    assert "protected" in out


def test_sessions_dump_labels_the_screening_estimate(adapter):
    out = adapter.HANDLERS["sessions"]({})
    assert "SCREENING" in out["note"]
    for row in out["sessions"]:
        assert "rss_estimate_gb" in row
