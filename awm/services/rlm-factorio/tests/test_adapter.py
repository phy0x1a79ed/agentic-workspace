import json

import pytest

from awm.rlm_factorio import appliance, hub_adapter as ha, throttle


class StubBroker:
    def __init__(self, output="", cost=0.1):
        self.output, self.cost, self.sent = output, cost, []

    def lua(self, conn, code, *, timeout):
        self.sent.append((conn, code))
        return {"output": self.output, "cost_ms": self.cost}


@pytest.fixture()
def stub(monkeypatch):
    b = StubBroker()
    monkeypatch.setattr(ha.broker, "for_row", lambda row: b)
    monkeypatch.setattr(ha, "THROTTLE", throttle.Throttle())
    return b


def test_iface_builds_remote_call_and_decodes(stub):
    stub.output = json.dumps({"ok": 1}) + "\n"
    result, cost = ha._iface({}, "observe", {"seat": "s'1"}, key="s'1")
    assert result == {"ok": 1} and cost == 0.1
    conn, code = stub.sent[0]
    assert conn == "s'1" and "remote.call('game_bot','observe'" in code and "s\\'1" in code


def test_iface_surfaces_lua_errors(stub):
    stub.output = "Cannot execute command. Error: boom\n"
    with pytest.raises(appliance.ApplianceError, match="boom"):
        ha._iface({}, "observe", {}, key="k")


def test_budgeted_wraps_but_keeps_result(stub):
    assert ha._budgeted("k", {"a": 1}, 3.0, 15) == {"a": 1}
    env = ha._budgeted("k", {"a": 1}, 20.0, 15)
    assert env["ok"] is False and env["ran"] is True and env["result"] == {"a": 1}
    assert env["error"].startswith("OVER_BUDGET: THIS COMMAND RAN; DO NOT RETRY")
    assert ha.THROTTLE.snapshot()["keys"]["k"]["over_budget"] == 1


def test_cap_output_marks_truncation():
    assert ha._cap_output("abc", 10) == "abc"
    capped = ha._cap_output("x" * 100, 10)
    assert capped.startswith("x" * 10) and "truncated: 10 of 100" in capped


def test_checked_validates_before_handler():
    seen = []
    run = ha._checked("move", lambda args: seen.append(args) or args)
    assert run({"x": "1.5", "y": 2}) == {"x": 1.5, "y": 2.0}
    with pytest.raises(ValueError, match="argument x"):
        run({"x": "east", "y": 2})
    assert len(seen) == 1


def test_checked_threads_identity_when_asked():
    run = ha._checked("join", lambda args, as_: as_)
    assert run({"session_id": "s"}, "agent-1") == "agent-1"


def test_every_handler_is_checked_and_manifested():
    names = {f["name"] for f in ha.API_MANIFEST["functions"]}
    assert set(ha.HANDLERS) == names
    assert all(fn.__name__ == "run" for fn in ha.HANDLERS.values())
