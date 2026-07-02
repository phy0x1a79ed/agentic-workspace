"""Accept leg (agents half) — the independent execution-verified acceptance gate.

The ``accept`` placement mode: an INDEPENDENT verifier with read + Bash-EXECUTE
(but NO Write/Edit) that runs the infra-owned ``accept_spec`` checks and calls
``accept_work`` / ``reject_work``. Mirrors the plan→verify leg one layer later.

Covers: the accept-mode tool + verb profile, ``ensure_verb_allowed``
confinement, the ``relay_accept_work`` / ``relay_reject_work`` B-op relays, and
the worker brief's authoritative acceptance-criteria + rework sections.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.agents import placement
from awm.agents.dao import AgentsDAO
# Reuse the canonical FakeOrch + seed helpers from the worker-tools suite (the
# FakeOrch now records accept_work / reject_work too).
from awm.tests.agent.test_worker_tools import FakeOrch, _seed, _data


@pytest.fixture
def fake_orch(agents_env):
    from awm.agents import orch_client
    impl = FakeOrch(agents_env["calls"])
    orch_client.set_impl(impl)
    yield impl
    orch_client.reset_impl()


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
    async def test_verbs_are_accept_reject_fail(self, agents_env, stub_core):
        res = await _place_accept(agents_env)
        data = json.loads(
            AgentsDAO().resolve_placement(res["placement_token"])["data"])
        assert set(data["allowed_verbs"]) == {
            "accept_work", "reject_work", "task_fail"}

    async def test_tools_read_and_bash_no_edit(self, agents_env, stub_core):
        await _place_accept(agents_env)
        cfg = stub_core["opened"][0]
        # read + run: the readonly built-ins AND Bash are present …
        for t in ("Read", "Grep", "Glob", "Bash"):
            assert t in cfg.allowed_tools
        # … but NO write surface — the verifier verifies, it never fixes.
        for t in ("Write", "Edit", "NotebookEdit"):
            assert t not in cfg.allowed_tools
        assert "mcp__awm__agent" in cfg.allowed_tools

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
    async def test_worker_verb_rejected_in_accept_mode(self, agents_env, stub_core):
        await _place_accept(agents_env)
        for verb in ("indicate_done", "edit_deliverable"):
            with pytest.raises(placement.PlacementError):
                placement.ensure_verb_allowed("leaf-a", verb)
        # its own verdict verbs pass
        placement.ensure_verb_allowed("leaf-a", "accept_work")
        placement.ensure_verb_allowed("leaf-a", "reject_work")

    async def test_accept_work_rejected_in_worker_mode(self, agents_env, stub_core):
        await placement.place_on_task({
            "task_id": "T-W", "unit_slug": "leaf-w", "brief": "do it",
            "contracts_out": ["out:1"], "mode": "worker"})
        with pytest.raises(placement.PlacementError):
            placement.ensure_verb_allowed("leaf-w", "accept_work")


class TestAcceptRelays:
    async def test_accept_work_relays_and_retires(self, agents_env, fake_orch):
        iid, _ = _seed(agents_env, token="plt-a", scope="leaf-a", mode="accept")
        res = await placement.relay_accept_work({"evidence": "ran all checks"},
                                                "leaf-a")
        assert res["outcome"] == "accepted"
        _o, _n, kw = next(c for c in agents_env["calls"] if c[1] == "accept_work")
        assert kw["task_id"] == "T-1" and kw["evidence"] == "ran all checks"
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("orch", "accept_work") in names
        # retire = close placement + retain the unit AFTER the orch ack.
        assert names.index(("orch", "accept_work")) < names.index(
            ("workspace", "workspace_retain"))
        # the placement row is closed (its terminal outcome recorded).
        assert _data(iid)["placement_outcome"] == "accepted"

    async def test_reject_work_relays_reason_and_retires(self, agents_env,
                                                         fake_orch):
        iid, _ = _seed(agents_env, token="plt-a", scope="leaf-a", mode="accept")
        res = await placement.relay_reject_work(
            {"reason": "check 'runs' failed: exit 1", "partial_ref": "art:p"},
            "leaf-a")
        assert res["outcome"] == "rejected"
        _o, _n, kw = next(c for c in agents_env["calls"] if c[1] == "reject_work")
        assert kw["reason"] == "check 'runs' failed: exit 1"
        assert kw["partial_ref"] == "art:p"
        names = [(c[0], c[1]) for c in agents_env["calls"]]
        assert ("orch", "reject_work") in names
        assert ("workspace", "workspace_retain") in names
        assert _data(iid)["placement_outcome"] == "rejected"


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
