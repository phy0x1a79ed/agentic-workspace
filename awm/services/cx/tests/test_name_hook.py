"""The hook that hands a claimed session back to Claude Code's own namer.

It runs on every prompt in every session on the node, so every case here is
really one question: does it leave that session alone?
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "name_on_prompt.py"


@pytest.fixture
def job(tmp_path):
    """A job directory holding a freshly claimed session's state record."""
    d = tmp_path / "jobs" / "abc123"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({
        "name": "claimed dunlin", "nameSource": "user", "intent": "",
        "cwd": "/home/tony", "state": "working",
    }))
    return d


def run(job, payload, **env):
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_JOB_DIR": str(job), **env})
    assert out.returncode == 0
    assert out.stdout == ""
    return json.loads((job / "state.json").read_text())


def test_a_first_prompt_clears_the_name_and_records_the_intent(job):
    state = run(job, {"source": "user", "prompt": "fix the flaky import test"})
    assert "name" not in state
    assert "nameSource" not in state
    assert state["intent"] == "fix the flaky import test"


def test_the_rest_of_the_record_is_left_exactly_as_it_was(job):
    state = run(job, {"source": "user", "prompt": "hello there"})
    assert state["cwd"] == "/home/tony"
    assert state["state"] == "working"


def test_a_slash_command_is_not_a_first_prompt(job):
    """`/cd` is typed into every session the pool hands out."""
    state = run(job, {"source": "user", "prompt": "/cd /home/tony/project"})
    assert state["name"] == "claimed dunlin"


def test_a_session_that_is_not_the_pool_s_is_untouched(job):
    (job / "state.json").write_text(json.dumps({"name": "remote shell"}))
    state = run(job, {"source": "user", "prompt": "carry on"})
    assert state == {"name": "remote shell"}


def test_a_session_that_has_already_been_titled_is_untouched(job):
    """The name the namer wrote must survive every prompt after the first."""
    (job / "state.json").write_text(json.dumps(
        {"name": "flaky import test", "nameSource": "auto"}))
    state = run(job, {"source": "user", "prompt": "and now the other one"})
    assert state["name"] == "flaky import test"


def test_a_wake_up_is_not_a_person_typing(job):
    state = run(job, {"source": "schedule_wakeup", "prompt": "check the run"})
    assert state["name"] == "claimed dunlin"


def test_injected_context_is_not_part_of_the_intent(job):
    state = run(job, {"source": "user", "prompt":
                      "<system-reminder>noise</system-reminder>  the real ask"})
    assert state["intent"] == "the real ask"


def test_a_long_prompt_is_truncated_the_way_the_vendor_truncates_it(job):
    state = run(job, {"source": "user", "prompt": "x" * 900})
    assert len(state["intent"]) == 500


def test_an_empty_intent_is_never_written(job):
    """`??` merges this field forward, so an empty string would stick for the
    life of the session and the namer would never fire."""
    state = run(job, {"source": "user", "prompt": "   "})
    assert state["name"] == "claimed dunlin"


def test_a_session_with_no_job_directory_is_a_no_op(job):
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"source": "user", "prompt": "hello"}),
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0
    assert json.loads((job / "state.json").read_text())["name"] == "claimed dunlin"


def test_garbage_on_standard_input_costs_nobody_a_prompt(job):
    out = subprocess.run(
        [sys.executable, str(HOOK)], input="not json at all",
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_JOB_DIR": str(job)})
    assert out.returncode == 0
    assert out.stdout == ""


def test_an_unreadable_state_record_costs_nobody_a_prompt(job):
    (job / "state.json").write_text("{ truncated")
    out = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"source": "user", "prompt": "hello"}),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_JOB_DIR": str(job)})
    assert out.returncode == 0
    assert (job / "state.json").read_text() == "{ truncated"


def test_the_hook_and_the_service_agree_on_the_claimed_prefix():
    """Two files have to say `claimed ` and the hook cannot import the other."""
    from awm.cx import config

    assert config.claimed_prefix() in HOOK.read_text()
