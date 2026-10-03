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


class ScriptedBroker(StubBroker):
    """Replies in turn from a list of outputs."""

    def __init__(self, outputs):
        super().__init__()
        self.outputs = list(outputs)

    def lua(self, conn, code, *, timeout):
        self.sent.append((conn, code))
        return {"output": self.outputs.pop(0), "cost_ms": 0.1}


@pytest.fixture()
def scripted(monkeypatch):
    def install(outputs):
        b = ScriptedBroker(outputs)
        monkeypatch.setattr(ha.broker, "for_row", lambda row: b)
        monkeypatch.setattr(ha, "THROTTLE", throttle.Throttle())
        ha._forget_mod_state("s1")
        return b
    return install


def test_orders_refused_before_mod_05(scripted):
    scripted(["0.4.1\n"])
    with pytest.raises(appliance.ApplianceError, match="mod >= 0.5"):
        ha._require_orders({"session_id": "s1"})


def test_stored_script_defines_once_then_runs_by_name(scripted):
    b = scripted(["hi\n", "hi\n"])
    ha._run_stored({"session_id": "s1"}, "k", "rcon.print('hi')", "seat-1")
    ha._run_stored({"session_id": "s1"}, "k", "rcon.print('hi')", "seat-1")
    first, second = b.sent[0][1], b.sent[1][1]
    assert "script_define" in first and "script_run" in first
    assert "script_define" not in second and 'args="seat-1"' in second
    assert len(second) < 120


def test_stored_script_redefines_after_world_reload(scripted):
    b = scripted(["hi\n", "Cannot execute command. Error: unknown script: x:\n", "hi\n"])
    ha._run_stored({"session_id": "s1"}, "k", "rcon.print('hi')", None)
    name = b.sent[0][1].split("name='")[1].split("'")[0]
    b.outputs[0] = f"Cannot execute command. Error: unknown script: {name}; define it\n"
    res = ha._run_stored({"session_id": "s1"}, "k", "rcon.print('hi')", None)
    assert res["output"] == "hi\n" and "script_define" in b.sent[2][1]


def test_await_order_polls_until_done(scripted, monkeypatch):
    monkeypatch.setattr(ha, "ORDER_POLL_S", 0)
    running = json.dumps({"orders": [{"id": 4, "status": "running"}]})
    done = json.dumps({"orders": [{"id": 4, "status": "arrived"}]})
    scripted([running, running, done])
    assert ha._await_order({}, "k", 4, 5.0, None)["status"] == "arrived"
