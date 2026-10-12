"""The reporting hook against a stub gateway, run the way Claude Code runs it: `python3 -S`, JSON on stdin."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HOOK = HERE / "reporting_hook.py"
INSTALLER = HERE.parents[2] / "scripts" / "deploy-hooks.sh"
URL = "https://nexus.example.org/trilium/#root/abc/def123456789"


class Gateway:
    def __init__(self):
        self.calls, self.reply = [], {"ok": True, "blocked": False}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.path, self.headers.get("X-Awm-Caller-Pid"), body))
                data = json.dumps({"result": outer.reply}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def gw():
    g = Gateway()
    yield g
    g.server.shutdown()


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".claude" / "plans").mkdir(parents=True)
    return tmp_path


def run(home, gw_url, payload, **env):
    out = subprocess.run(
        [sys.executable, "-S", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "AWM_HUB_URL": gw_url, **env}, timeout=30)
    assert out.returncode == 0 and out.stdout == ""
    return out


def post(tool, **kw):
    return {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": tool, **kw}


def prompt(text):
    return {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": text}


def marker(home):
    return home / ".claude" / "awm-reporting" / "s1.blocked"


def test_a_plan_write_sends_the_file_text(home, gw):
    plan = home / ".claude" / "plans" / "a-plan.md"
    plan.write_text("# plan")
    for tool in ("Write", "Edit"):
        run(home, gw.url, post(tool, tool_input={"file_path": str(plan)}))
    path, pid, body = gw.calls[0]
    assert path == "/invoke" and pid.isdigit()
    assert body == {"name": "reporting_hook_plan",
                    "args": {"session_id": "s1", "plan_path": str(plan), "plan_text": "# plan"}}
    assert len(gw.calls) == 2


def test_other_writes_and_subagent_plans_are_ignored(home, gw):
    plan = home / ".claude" / "plans" / "a-plan.md"
    plan.write_text("# plan")
    run(home, gw.url, post("Write", tool_input={"file_path": str(plan)}, agent_id="sub1"))
    run(home, gw.url, post("Write", tool_input={"file_path": str(home / "notes.md")}))
    run(home, gw.url, post("Write", tool_input={"file_path": str(home / ".claude" / "plans" / "x.txt")}))
    assert gw.calls == []


def test_an_approved_plan_is_sent_and_a_bare_response_is_not(home, gw):
    run(home, gw.url, post("ExitPlanMode", tool_response={"plan": "# approved", "isAgent": False}))
    run(home, gw.url, post("ExitPlanMode", tool_response={"status": "denied"}))
    run(home, gw.url, post("ExitPlanMode", tool_response={"plan": "# sub"}, agent_id="sub1"))
    assert [c[2] for c in gw.calls] == [
        {"name": "reporting_hook_plan_approved", "args": {"session_id": "s1", "plan_text": "# approved"}}]


def test_task_events_send_the_whole_list_from_disk(home, gw):
    d = home / ".claude" / "tasks" / "s1"
    d.mkdir(parents=True)
    for i, (status, subject) in enumerate([("completed", "T1"), ("pending", "T2")] * 6, 1):
        (d / f"{i}.json").write_text(json.dumps({"id": str(i), "subject": f"{subject}.{i}", "status": status}))
    (d / ".lock").write_text("")
    (d / "bad.json").write_text("{")
    run(home, gw.url, post("TaskUpdate", tool_input={"taskId": "2", "status": "completed"}))
    args = gw.calls[0][2]["args"]
    assert gw.calls[0][2]["name"] == "reporting_hook_todos"
    assert [i["id"] for i in args["items"]] == [str(n) for n in range(1, 13)]
    assert args["items"][0] == {"id": "1", "subject": "T1.1", "status": "completed"}


def test_the_task_list_id_override_is_honoured(home, gw):
    d = home / ".claude" / "tasks" / "named-list"
    d.mkdir(parents=True)
    (d / "1.json").write_text(json.dumps({"id": "1", "subject": "x", "status": "pending"}))
    run(home, gw.url, post("TaskCreate", tool_input={}), CLAUDE_CODE_TASK_LIST_ID="named-list")
    assert len(gw.calls) == 1


def test_deleting_the_last_task_sends_an_empty_list(home, gw):
    (home / ".claude" / "tasks" / "s1").mkdir(parents=True)
    run(home, gw.url, post("TaskUpdate", tool_input={"taskId": "1", "status": "deleted"}))
    assert gw.calls[0][2] == {"name": "reporting_hook_todos", "args": {"session_id": "s1", "items": []}}


def test_a_cleared_list_sends_nothing(home, gw):
    run(home, gw.url, post("TaskUpdate", tool_input={"taskId": "1", "status": "completed"}))
    (home / ".claude" / "tasks" / "s1").mkdir(parents=True)
    run(home, gw.url, post("TaskUpdate", tool_input={"taskId": "1", "status": "completed"}))
    run(home, gw.url, post("TodoWrite", tool_input={"todos": []}))
    assert gw.calls == []


def test_todowrite_carries_the_list_in_its_input(home, gw):
    todos = [{"content": "a", "status": "pending", "activeForm": "A"},
             {"content": "b", "status": "completed", "activeForm": "B"}]
    run(home, gw.url, post("TodoWrite", tool_input={"todos": todos}))
    assert gw.calls[0][2]["args"]["items"] == [
        {"id": "1", "subject": "a", "status": "pending"}, {"id": "2", "subject": "b", "status": "completed"}]


def test_a_plain_prompt_makes_no_call(home, gw):
    run(home, gw.url, prompt("fix the build"))
    run(home, gw.url, prompt("see https://example.org/page#top"))
    run(home, gw.url, prompt("read " + URL + " first"))
    assert gw.calls == []


def test_a_prompt_with_a_note_url_is_sent(home, gw):
    run(home, gw.url, prompt("claim task " + URL + " please"))
    assert gw.calls[0][2] == {"name": "reporting_hook_prompt",
                              "args": {"session_id": "s1", "prompt": "claim task " + URL + " please"}}


def test_a_claim_of_a_nexus_url_without_a_trilium_path_is_sent(home, gw):
    run(home, gw.url, prompt("attach to https://nexus.example.org/#root/abc/def123456789"))
    assert len(gw.calls) == 1


def test_the_blocked_marker_follows_the_result(home, gw):
    plan = home / ".claude" / "plans" / "p.md"
    plan.write_text("x")
    gw.reply = {"ok": True, "blocked": True}
    run(home, gw.url, post("Write", tool_input={"file_path": str(plan)}))
    assert marker(home).exists()
    gw.reply = {"ok": True}
    run(home, gw.url, prompt("while blocked"))
    assert len(gw.calls) == 2 and marker(home).exists()
    gw.reply = {"ok": True, "blocked": False}
    run(home, gw.url, prompt("answering the question"))
    assert not marker(home).exists()
    run(home, gw.url, prompt("later"))
    assert len(gw.calls) == 3


def test_a_dead_gateway_still_exits_zero_and_keeps_the_marker(home):
    marker(home).parent.mkdir(parents=True)
    marker(home).touch()
    run(home, "http://127.0.0.1:9", prompt("x"))
    assert marker(home).exists()


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"hook_event_name": "UserPromptSubmit"}'])
def test_garbage_input_exits_zero(home, raw):
    out = subprocess.run([sys.executable, "-S", str(HOOK)], input=raw, capture_output=True, text=True,
                         env={"HOME": str(home)})
    assert out.returncode == 0 and out.stdout == ""


def test_the_installer_deploys_idempotently(tmp_path):
    script = tmp_path / "awm" / "scripts" / "deploy-hooks.sh"
    script.parent.mkdir(parents=True)
    (tmp_path / "awm" / "services").symlink_to(INSTALLER.parents[1] / "services")
    script.write_text(re.sub(r'\|(local[^|]*)\|', "|local|", INSTALLER.read_text()))
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "other"}]}]}}))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}

    def deploy(*a):
        return subprocess.run(["bash", str(script), *a], capture_output=True, text=True, env=env)

    assert deploy("--check").returncode == 1
    assert deploy().returncode == 0
    again = deploy("--check")
    assert again.returncode == 0, again.stdout
    settings = json.loads((claude / "settings.json").read_text())
    reporting = settings["hooks"]["PostToolUse"][-1]
    assert reporting["hooks"][0]["command"] == f"python3 -S {tmp_path}/.claude/hooks/awm-reporting-hook.py 2>/dev/null || true"
    assert {"hooks": [{"command": "other"}]} in settings["hooks"]["PostToolUse"]
    standalone = json.loads((claude / "hooks" / "awm-reporting-settings.json").read_text())
    assert set(standalone["hooks"]) == {"PostToolUse", "UserPromptSubmit"}
