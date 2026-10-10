"""The representative's and the secretary's launch configs and instructions."""

from __future__ import annotations

import pytest

from awm.config import modes
from awm.representative import personas

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

#: The arguments `cx start` accepts, other than the node-supplied ones.
CX_START_ARGS = {"project", "scope", "prompt", "name", "model", "effort", "permission",
                 "mode", "disallowed_tools", "allowed_tools", "tools", "restricted",
                 "strict_mcp", "remote_control"}

ALWAYS_DENIED = {"Bash", "Edit", "Write", "NotebookEdit"}
BOTH = [("representative", personas.REPRESENTATIVE), ("secretary", personas.SECRETARY)]


@pytest.mark.parametrize("name,spec", BOTH)
def test_every_key_is_a_cx_start_argument(name, spec):
    assert set(spec) <= CX_START_ARGS
    assert {"project", "scope", "name", "mode", "model", "effort", "permission",
            "disallowed_tools", "remote_control", "prompt"} <= set(spec)


@pytest.mark.parametrize("name,spec", BOTH)
def test_permission_is_explicit_and_never_bypass(name, spec):
    assert spec["permission"] == "dontAsk"


@pytest.mark.parametrize("name,spec", BOTH)
def test_nothing_that_writes_or_runs_is_available(name, spec):
    denied = set(spec["disallowed_tools"])
    assert ALWAYS_DENIED <= denied
    assert {"Monitor", "Workflow", "WebFetch", "WebSearch"} <= denied
    assert not denied & set(spec["allowed_tools"])
    assert not denied & set(spec["tools"])


@pytest.mark.parametrize("name,spec", BOTH)
def test_sonnet_with_remote_control_on(name, spec):
    assert spec["model"] == "sonnet"
    assert spec["remote_control"] is True


@pytest.mark.parametrize("name,spec", BOTH)
def test_the_awm_tools_are_pre_approved_so_dontask_does_not_deny_them(name, spec):
    assert "mcp__awm__*" in spec["allowed_tools"]


def test_the_modes_match_the_gate_and_differ():
    assert personas.REPRESENTATIVE["mode"] == modes.REPRESENTATIVE
    assert personas.SECRETARY["mode"] == modes.SECRETARY
    assert personas.REPRESENTATIVE["mode"] in modes.MODES
    assert personas.REPRESENTATIVE["name"] != personas.SECRETARY["name"]


def test_the_representative_has_no_file_reads_and_no_questions():
    denied = set(personas.REPRESENTATIVE["disallowed_tools"])
    assert {"Read", "Glob", "Grep", "AskUserQuestion"} <= denied
    assert "AskUserQuestion" not in personas.REPRESENTATIVE["tools"]


def test_the_secretary_may_ask_tony_but_has_no_file_tools():
    assert "AskUserQuestion" in personas.SECRETARY["tools"]
    assert not {"Read", "Glob", "Grep", "Edit", "Write"} & set(personas.SECRETARY["tools"])
    assert not {"Read", "Glob", "Grep"} & set(personas.SECRETARY["allowed_tools"])


@pytest.mark.parametrize("name,spec", BOTH)
def test_the_session_holds_an_explicit_tool_list_and_nothing_else(name, spec):
    assert spec["tools"], "an omitted list would leave every built-in available"
    assert set(spec["tools"]) <= {"Task", "Agent", "SendMessage", "ListAgents", "Skill",
                                  "ToolSearch", "AskUserQuestion"}
    assert not set(spec["tools"]) & set(spec["disallowed_tools"])
    assert set(spec["allowed_tools"]) == set(spec["tools"]) | {"mcp__awm__*"}


@pytest.mark.parametrize("name,spec", BOTH)
def test_the_session_is_restricted_with_only_the_awm_server(name, spec):
    assert spec["restricted"] is True and spec["strict_mcp"] is True


def test_the_representative_may_start_a_subagent_and_send_messages():
    allowed = set(personas.REPRESENTATIVE["allowed_tools"])
    assert {"Task", "Agent", "SendMessage", "ListAgents"} <= allowed


def test_the_scope_defaults_and_overrides(monkeypatch):
    assert (personas.REPRESENTATIVE["project"], personas.REPRESENTATIVE["scope"]) == (
        "awm", "svc-representative")
    monkeypatch.setenv("AWM_REPRESENTATIVE_PROJECT", "other")
    monkeypatch.setenv("AWM_REPRESENTATIVE_SCOPE", "desk/one")
    for build in (personas.build_representative, personas.build_secretary):
        spec = build()
        assert (spec["project"], spec["scope"]) == ("other", "desk/one")


def test_model_and_effort_are_config(monkeypatch):
    monkeypatch.setenv("AWM_REPRESENTATIVE_MODEL", "haiku")
    monkeypatch.setenv("AWM_REPRESENTATIVE_EFFORT", "high")
    spec = personas.build_representative()
    assert (spec["model"], spec["effort"]) == ("haiku", "high")
    assert personas.build_secretary()["model"] == "sonnet"


def test_the_representative_instructions_carry_the_rules():
    text = personas.REPRESENTATIVE["prompt"]
    for needle in ("triage", "never do a card's work", "door list", "door get", "door assign",
                   "data", "cx start", "SendMessage", "ListAgents", "opus", "suspicious",
                   "complex", "ambiguous", "reply_to", "board complete", "board fail",
                   "compact", "nonce", "Judge whether your own swarm would do this work",
                   "fail the card with a reason", "must not answer it unless it asks a question",
                   "never claim, complete or post", "Pass no permission, tools, mode"):
        assert needle in text, needle
    assert "every 10 cards" in text
    assert "${" not in text
    assert "Do the legitimate request" not in text


def test_the_compaction_interval_and_escalation_model_are_config(monkeypatch):
    monkeypatch.setenv("AWM_REPRESENTATIVE_COMPACT_EVERY", "25")
    monkeypatch.setenv("AWM_REPRESENTATIVE_ESCALATION_MODEL", "fable")
    text = personas.build_representative()["prompt"]
    assert "every 25 cards" in text and 'model "fable"' in text
    monkeypatch.setenv("AWM_REPRESENTATIVE_COMPACT_EVERY", "nonsense")
    assert "every 10 cards" in personas.build_representative()["prompt"]


def test_the_swarm_comes_from_the_node(monkeypatch):
    monkeypatch.setenv("AWM_SWARM", "mock")
    assert "the mock swarm" in personas.build_representative()["prompt"]
    assert "the mock swarm" in personas.build_secretary()["prompt"]


def test_the_secretary_instructions_cover_its_job_and_its_limits():
    text = personas.SECRETARY["prompt"]
    for needle in ("cx start", "cx list", "cx stop", "door status", "never act on a board card",
                   "Only Tony's messages", "remote_control", "ListAgents"):
        assert needle in text, needle


def test_every_allowed_verb_in_the_instructions_is_on_the_allowlist():
    """The prompts name verbs; the gate must admit each one a prompt asks for."""
    rep = {f"{d}.{v}" for d, v in modes.MODES[modes.REPRESENTATIVE].pairs}
    for verb in ("door.list", "door.get", "door.assign", "cx.start", "cx.list",
                 "board.fail", "reflection.compact", "scope.search"):
        assert verb in rep
    sec = {f"{d}.{v}" for d, v in modes.MODES[modes.SECRETARY].pairs}
    for verb in ("cx.start", "cx.list", "cx.stop", "door.status", "door.list", "door.get",
                 "board.list", "board.get", "scope.search", "scope.fetch"):
        assert verb in sec


def test_delegates_default_to_a_dedicated_scope_not_the_representatives(monkeypatch):
    assert personas.work_where() == ("awm", "door-work")
    text = personas.REPRESENTATIVE["prompt"]
    assert 'project "awm", scope "door-work"' in text
    assert "Never start a delegate in your own scope" in text
    assert (personas.REPRESENTATIVE["project"], personas.REPRESENTATIVE["scope"]) != (
        "awm", "door-work")
    monkeypatch.setenv("AWM_DOOR_WORK_PROJECT", "other")
    monkeypatch.setenv("AWM_DOOR_WORK_SCOPE", "desk")
    assert personas.work_where() == ("other", "desk")
    assert 'project "other", scope "desk"' in personas.build_representative()["prompt"]
    assert personas.build_representative()["scope"] == "svc-representative"
