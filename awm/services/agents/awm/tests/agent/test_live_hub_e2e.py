"""Live-hub lifecycle e2e — PRIMED FOR NEXT SESSION (skipped by default).

The in-process ``test_lifecycle_e2e`` already proves the placement runtime end to
end against a stubbed orchestrator with a fake subprocess. The remaining gap is a
*real* run: a dev hub on :7821 bootstrapping the workspace + agents services, a
real ``claude`` spawned per stage, and a stub orchestrator service answering the
B-ops. That needs live processes (not a pure unit), so it is staged here and
skipped until run by hand next session.

Runbook (next session)
----------------------
1. Install the new service into the env (once):
       bash awm/services/workspace/install.sh

2. Bring up an ISOLATED dev hub (never prod :7819) with the workspace + agents
   services, following the modular isolated harness pattern
   (memory: awm_modular_isolated_test_harness):
       - boot the modular gateway from the worktree via PYTHONPATH dist-roots
       - AWM_WORKSPACE=/tmp/awm-life-e2e, a spare AWM_PORT (e.g. 7821)
       - the gateway filesystem-discovers awm/services/{workspace,agents}

3. Register a STUB orchestrator service exposing the manifest-omitted B-ops
   (deliver/fail/decompose_commit/approve_plan/reject_plan/set_attached/
   search_tasks/search_contracts) that just records calls and returns acks +
   drives the state transitions (PLANNING→VERIFYING_PLAN on deliver("plan"),
   →ACTIVE on approve_plan). The agents service reaches it unchanged via
   orch_client's live gateway default.

4. Drive the lifecycle by POSTing to the gateway /invoke endpoint
   (memory: awm_invoke_args_key — params go under "args", NOT "arguments"):
       a. place_on_task {task_id, project, unit_slug, brief, mode: "plan"}
       b. wait for the spawned claude to edit_deliverable("plan", …) +
          indicate_done; the supervisor's stop-boundary check fires deliver("plan")
       c. place_on_task {…, unit_slug (SAME), mode: "verify"}; the verifier
          approves → approve_plan
       d. place_on_task {…, unit_slug (SAME), mode: "worker",
          contracts_out: [...]}; worker stages + delivers

5. Assert the stub orchestrator saw, in order: deliver("plan") → approve_plan →
   deliver(<real contract>), and that the unit dir was reused (one unit, scopes
   service never touched).

Un-skip the test below (or convert this runbook into a driver script) once the
dev hub is standing.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.skip(reason="live dev hub + real claude — run by hand next session "
                         "(see module docstring runbook)")
async def test_live_hub_lifecycle():
    # Placeholder: the runbook above is the executable spec. Implement against a
    # standing dev hub (:7821) + stub orchestrator service next session.
    raise NotImplementedError
