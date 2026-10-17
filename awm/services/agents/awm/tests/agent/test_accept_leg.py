"""Accept leg (agents half) — the independent execution-verified acceptance gate.

The ``accept`` placement mode: an INDEPENDENT verifier with read + Bash-EXECUTE
(but NO Write/Edit) that runs the infra-owned ``accept_spec`` checks and calls
``accept_work`` / ``reject_work``. Mirrors the plan→verify leg one layer later.

Covers the AGENTS half: the accept-mode tool profile (read + Bash, no edit),
the empty agent-domain verb profile (the accept VERDICTS moved to the
orchestrator's ``orchwork`` domain — the lift, T4a), and the worker brief's
authoritative acceptance-criteria + rework sections. The accept_work /
reject_work verdicts themselves are covered by the orchestrator's own
``test_worker_tools``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents.dao import AgentsDAO


_SPEC = {
    "objective": "The fabfos tool exits 0 and prints DONE.",
    "checks": [
        {"name": "runs", "cmd": "python tool.py", "expect_exit": 0},
        {"name": "prints-done", "cmd": "python tool.py",
         "expect_output": "DONE"},
    ],
}


def _accept_brief(**over):
    d = {"goal": "build the tool", "accept_spec": _SPEC, "produces": ["out:1"]}
    d.update(over)
    return json.dumps(d)


async def _place_accept(agents_env, **over):
    args = {"task_id": "T-A", "unit_slug": "leaf-a",
            "brief": _accept_brief(), "contracts_in": ["in:1"],
            "contracts_out": [], "mode": "accept", "prereadings": []}
    args.update(over)
    return await placement.place_on_task(args)


class TestAcceptModeProfile:
    async def test_no_agent_domain_verbs(self, agents_env, stub_core):
        # The accept verdicts (accept_work / reject_work / task_fail) moved to the
        # orchestrator's ``orchwork`` domain, so an accept placement records NO
        # agent-domain verbs — its verdicts are gated orchestrator-side now.
        res = await _place_accept(agents_env)
        data = json.loads(
            AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert set(data["allowed_verbs"]) == set()

    async def test_tools_read_and_bash_no_edit(self, agents_env, stub_core):
        await _place_accept(agents_env)
        cfg = stub_core["opened"][0]
        # read + run: the readonly built-ins AND Bash are present …
        for t in ("Read", "Grep", "Glob", "Bash"):
            assert t in cfg.allowed_tools
        # … but NO write surface — the verifier verifies, it never fixes.
        for t in ("Write", "Edit", "NotebookEdit"):
            assert t not in cfg.allowed_tools
        # The verdicts live on the orchestrator's ``orchwork`` domain now — the
        # accept placement carries that domain, NOT the agents ``agent`` one.
        assert "mcp__awm__orchwork" in cfg.allowed_tools
        assert "mcp__awm__agent" not in cfg.allowed_tools

    async def test_brief_has_checks_and_readonly_note(self, agents_env, stub_core):
        await _place_accept(agents_env)
        ctx = next(a for (svc, fn, a) in agents_env["calls"]
                   if fn == "workspace_create")["context_md"]
        # each check name + cmd is quoted verbatim
        assert "runs" in ctx and "python tool.py" in ctx
        assert "prints-done" in ctx
        # the cannot-edit / verify-only note is present
        assert "CANNOT" in ctx and "verify" in ctx.lower()
        assert "accept_work" in ctx and "reject_work" in ctx
        # accept is never attended → no steering section
        assert "Steering — how to finish alignment" not in ctx


class TestVerbConfinement:
    async def test_agent_domain_verbs_all_rejected_in_accept_mode(
            self, agents_env, stub_core):
        # An accept placement hosts NO agent-domain verb — the worker-staging
        # verbs and the (moved) verdict verbs alike are rejected by the agents
        # gate; the verdicts are permitted only on the orchestrator's orchwork
        # gate.
        await _place_accept(agents_env)
        for verb in ("indicate_done", "edit_deliverable",
                     "accept_work", "reject_work", "task_fail"):
            with pytest.raises(placement.PlacementError):
                placement.ensure_verb_allowed("leaf-a", verb)

    async def test_accept_work_rejected_in_worker_mode(self, agents_env, stub_core):
        await placement.place_on_task({
            "task_id": "T-W", "unit_slug": "leaf-w", "brief": "do it",
            "contracts_out": ["out:1"], "mode": "worker"})
        with pytest.raises(placement.PlacementError):
            placement.ensure_verb_allowed("leaf-w", "accept_work")


class TestWorkerBriefAcceptance:
    """A gated worker's brief quotes the authoritative acceptance criteria
    verbatim, and renders the rework feedback after a reject_work."""

    def _brief(self, **over):
        args = dict(task_id="T-1", mode="worker", brief="build the tool",
                    contracts_in=[], contracts_out=["out:1"], prereadings=[],
                    attached=False, objective="", accept_spec=None,
                    rework_reason=None)
        args.update(over)
        return placement.render_brief(**args)

    def test_authoritative_criteria_section_present(self):
        brief = self._brief(accept_spec=_SPEC)
        assert ("## Acceptance criteria (authoritative — you may NOT change or "
                "narrow these)") in brief
        # objective + each check quoted verbatim
        assert _SPEC["objective"] in brief
        assert "runs" in brief and "python tool.py" in brief

    def test_no_criteria_section_when_ungated(self):
        # NULL accept_spec ⇒ legacy path ⇒ no acceptance-criteria section.
        brief = self._brief(accept_spec=None)
        assert "Acceptance criteria (authoritative" not in brief

    def test_rework_reason_rendered(self):
        brief = self._brief(
            accept_spec=_SPEC,
            rework_reason={"reason_text": "the DONE line was missing",
                           "partial_ref": "art:prev"})
        assert "Rework — your previous delivery was REJECTED" in brief
        assert "the DONE line was missing" in brief
        assert "art:prev" in brief


async def test_place_accept_kicks_off(agents_env, stub_core):
    # An accept placement is never attended → it kicks off immediately (no
    # wait-for-user), like a detached worker.
    await _place_accept(agents_env)
    await asyncio.sleep(0.05)
    assert any("Begin now" in s for s in stub_core["session"].sent)
