"""The Python gateway sinks plugin-reported usage into the day's JSONL file (contract § Usage event)."""

from __future__ import annotations

import json

import pytest

from awm.gateway import usage_log
from awm.gateway.hub.registry import ServiceRecord


def _lines(awm_dir):
    files = sorted((awm_dir / "usage").glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().splitlines()]


@pytest.fixture
def awm_dir(awm_workspace):
    import awm.config as cfg
    return cfg.AWM_DIR


def test_an_event_is_stamped_and_appended_as_one_line(awm_dir):
    assert usage_log.append({"plugin": "kb", "surface": "code", "name": "vectors"})
    assert usage_log.append({"plugin": "kb", "surface": "code", "name": "hops"})
    first, second = _lines(awm_dir)
    assert first["v"] == 1 and first["ts"].endswith("Z")
    assert {k: first[k] for k in ("plugin", "surface", "name", "tool", "ok")} == {
        "plugin": "kb", "surface": "code", "name": "vectors", "tool": None, "ok": None}
    assert second["name"] == "hops"


@pytest.mark.parametrize("raw", [
    {"surface": "code"},
    {"plugin": "kb", "surface": "email"},
    {"plugin": "kb", "surface": "code", "ok": "yes"},
    {"plugin": "kb", "surface": "code", "name": "x" * 5000},
])
def test_a_bad_event_is_dropped_without_raising(awm_dir, raw):
    assert usage_log.append(raw) is False
    assert not (awm_dir / "usage").exists() or not list((awm_dir / "usage").glob("*.jsonl"))


def test_a_control_frame_counts_under_the_service_never_the_frame(awm_dir, monkeypatch):
    from awm.gateway.api import hub
    from awm.gateway.hub import registry

    reg = registry.Registry()
    rec = ServiceRecord(name="transcripts", prefix="/svc/transcripts", kind="service")
    reg._install_base(rec)
    monkeypatch.setattr(hub, "get_registry", lambda: reg)
    ch = hub.rpc.ensure_control(rec.service_id)
    try:
        hub._route_inbound(ch, rec.service_id, {"kind": "usage", "surface": "background",
                                                "plugin": "journal", "name": "nightly",
                                                "ok": True, "ms": 840})
        hub._route_inbound(ch, "no-such-service", {"kind": "usage", "name": "x"})
    finally:
        hub.rpc.drop_control(rec.service_id)
    (event,) = _lines(awm_dir)
    assert (event["plugin"], event["surface"], event["name"], event["ok"], event["ms"]) == (
        "transcripts", "background", "nightly", True, 840)


def test_post_usage_takes_a_code_event_and_refuses_a_bad_one(awm_dir):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app

    with TestClient(app) as client:
        assert client.post("/usage", json={"plugin": "kb", "surface": "code", "name": "vectors"}).json() == {"ok": True}
        assert client.post("/usage", json={"surface": "code"}).status_code == 400
        assert client.post("/usage", content=b"[]", headers={"Content-Type": "application/json"}).status_code == 400
    (event,) = _lines(awm_dir)
    assert event["plugin"] == "kb"
