"""dsh's harness-contract verbs against a fake fork RPC API."""
from __future__ import annotations

import pytest

from awm.dsh import sessions

pytestmark = [pytest.mark.unit]

UUID = "0e380e38-0e38-4e38-8e38-0e380e380e38"
FORK = f"session-{UUID}"
R9 = "ds" + UUID.replace("-", "")


class Fork:
    def __init__(self):
        self.calls = []
        self.items = [{"sessionId": FORK, "running": False, "title": "web",
                       "cwd": "/w/projects/awm/redux-harness", "agentPreset": "code"},
                      {"sessionId": "odd-id", "running": True}]
        self.history = []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "session.list":
            return {"items": self.items}
        if method == "session.history":
            return {"events": self.history}
        return {"accepted": True}


@pytest.fixture
def fork(monkeypatch):
    f = Fork()
    monkeypatch.setattr(sessions, "_rpc", f)
    events = []
    monkeypatch.setattr(sessions, "_event", lambda s, fn, a: events.append(a))
    f.events = events
    return f


def test_the_r9_id_is_ds_and_the_uuid_hex():
    assert sessions.r9(FORK) == R9
    assert sessions.r9("odd-id") is None


def test_list_leaves_out_a_session_with_no_uuid(fork):
    [row] = sessions.rows()
    assert (row["session_id"], row["project"], row["scope"], row["state"]) == (
        R9, "awm", "redux-harness", "idle")


def test_find_matches_the_r9_id_a_prefix_the_fork_id_and_the_title(fork):
    for ref in (R9, R9[:10], FORK, "WEB"):
        assert sessions.find_one(ref)["session_id"] == R9, ref
    assert sessions.find_one("ds0e") is None


def test_plain_text_is_queued_and_a_slash_command_is_executed(fork):
    row = sessions.find_one(R9)
    assert sessions.send(row, "hello")["confirmed"] == "enqueued"
    assert fork.calls[-1] == ("session.prompt", {
        "sessionId": FORK, "mode": "queue", "content": [{"type": "text", "text": "hello"}]})
    sessions.send(row, "/compact")
    assert fork.calls[-1] == ("commands/execute", {"agentId": FORK, "line": "/compact"})


def test_stop_cancels_and_says_it_cannot_unload(fork):
    out = sessions.stop(sessions.find_one(R9))
    assert out["stopped"] and out["unloaded"] is False
    assert fork.calls[-1][0] == "session.cancel"


def test_set_cannot_move_a_session(fork):
    assert sessions.set_(sessions.find_one(R9), {"cwd": "/x"}, None)["unsupported"]


def test_model_and_effort_go_in_one_select(fork):
    sessions.set_(sessions.find_one(R9), {"model": "deepseek-v4", "effort": "high"}, "or")
    assert fork.calls[-1] == ("session.selectModel", {
        "sessionId": FORK, "provider": "or", "model": "deepseek-v4",
        "reasoningEffort": "high"})


def test_start_preallocates_the_id_so_it_knows_the_r9_first(fork, tmp_path):
    (tmp_path / "projects" / "awm" / "x").mkdir(parents=True)
    out = sessions.start({"project": "awm", "scope": "x", "name": "n", "prompt": "go"},
                         workspace=tmp_path, provider=None)
    method, payload = fork.calls[0]
    assert method == "session.create" and payload["sessionId"] == out["fork_session_id"]
    assert out["session_id"] == sessions.r9(out["fork_session_id"])
    assert [m for m, _ in fork.calls] == ["session.create", "session.rename",
                                           "session.prompt"]


def test_start_refuses_a_missing_worktree_and_pins(fork, tmp_path):
    assert sessions.start({"project": "a", "scope": "b"}, workspace=tmp_path,
                          provider=None)["ok"] is False
    assert sessions.start({"project": "a", "scope": "b", "pin": True},
                          workspace=tmp_path, provider=None)["unsupported"]


def test_compact_queues_the_followup_after_compaction_end_and_reports_done(fork):
    fork.history = [{"seq": 4, "type": "message"}]
    spawned = []
    out = sessions.compact(sessions.find_one(R9), "carry on", spawn=spawned.append)
    assert out["ticket"] and out["followup_deferred"]
    fork.history += [{"seq": 5, "type": "compaction/start"},
                     {"seq": 6, "type": "compaction/end", "payload": {}}]
    spawned[0]()
    assert fork.calls[-1][1]["content"][0]["text"] == "carry on"
    assert fork.events[-1]["kind"] == "done" and fork.events[-1]["ticket"] == out["ticket"]


def test_a_failed_compaction_is_lost_and_types_nothing(fork):
    row = sessions.find_one(R9)
    fork.history = [{"seq": 9, "type": "compaction/end", "payload": {"error": "overflow"}}]
    assert sessions.watch_compaction(row, "carry on", "t", 8, sleep=lambda s: None) == "lost"
    assert "overflow" in fork.events[-1]["reason"]
    assert not any(m == "session.prompt" for m, _ in fork.calls)


def test_a_compaction_that_never_ends_is_lost_at_the_deadline(fork, monkeypatch):
    ticks = iter(range(0, 10**6, 600))
    assert sessions.watch_compaction(sessions.find_one(R9), "x", "t", None,
                                     sleep=lambda s: None,
                                     clock=lambda: next(ticks)) == "lost"
    assert "did not end" in fork.events[-1]["reason"]


def test_a_minimal_preset_session_cannot_compact(fork):
    fork.items[0]["agentPreset"] = "minimal"
    assert sessions.compact(sessions.find_one(R9), "x")["unsupported"]


def test_resolve_reads_the_bash_tools_session_id(monkeypatch):
    env = {100: {"DSH_SESSION_ID": FORK}, 200: {}}
    parents = {300: 200, 200: 100, 100: 1}
    monkeypatch.setattr(sessions, "_environ", lambda pid: env.get(pid, {}))
    monkeypatch.setattr(sessions, "_ppid", lambda pid: parents.get(pid, 0))
    out = sessions.resolve(300, fork_pid=50)
    assert out["mine"] and out["session_id"] == R9


def _fork_rows(monkeypatch, *running):
    items = [{"sessionId": f"session-{u}", "running": u in running, "cwd": "/w"}
             for u in ("0" * 8 + "-0000-4000-8000-" + "0" * 12,
                       "1" * 8 + "-1111-4111-8111-" + "1" * 12)]
    monkeypatch.setattr(sessions, "all_sessions", lambda: items)
    return items


def test_resolve_owns_the_fork_pid_but_cannot_name_an_idle_caller(monkeypatch):
    monkeypatch.setattr(sessions, "_environ", lambda pid: {})
    monkeypatch.setattr(sessions, "_ppid", lambda pid: {60: 50}.get(pid, 0))
    _fork_rows(monkeypatch)
    out = sessions.resolve(60, fork_pid=50)
    assert out["mine"] and out["session_id"] is None and "MCP client" in out["error"]


def test_the_only_running_session_is_the_mcp_caller(monkeypatch):
    monkeypatch.setattr(sessions, "_environ", lambda pid: {})
    monkeypatch.setattr(sessions, "_ppid", lambda pid: {60: 50}.get(pid, 0))
    items = _fork_rows(monkeypatch, "1" * 8 + "-1111-4111-8111-" + "1" * 12)
    out = sessions.resolve(60, fork_pid=50)
    assert out["mine"] and out["session_id"] == sessions.r9(items[1]["sessionId"])


def test_two_running_sessions_name_no_caller(monkeypatch):
    monkeypatch.setattr(sessions, "_environ", lambda pid: {})
    monkeypatch.setattr(sessions, "_ppid", lambda pid: {60: 50}.get(pid, 0))
    _fork_rows(monkeypatch, "0" * 8 + "-0000-4000-8000-" + "0" * 12,
               "1" * 8 + "-1111-4111-8111-" + "1" * 12)
    out = sessions.resolve(60, fork_pid=50)
    assert out["session_id"] is None and "2 sessions" in out["error"]


def test_resolve_disowns_a_stranger(monkeypatch):
    monkeypatch.setattr(sessions, "_environ", lambda pid: {})
    monkeypatch.setattr(sessions, "_ppid", lambda pid: 1)
    assert sessions.resolve(77, fork_pid=50) == {"ok": True, "mine": False}
