"""Live-hub seamless-drop-in e2e — PRIMED FOR A MANUAL RUN (skipped by default).

The in-process ``test_lifecycle_e2e`` proves the placement runtime end to end
against a stubbed orchestrator with a fake subprocess, and the unit/integration
suites cover ``orch_node_open`` (orchestrator ``test_node_open``), the attach-gated
admin registry (orchestrator ``test_admin_ops`` + agents ``test_admin_ops``), and
the workspace canonical home (``workspace`` dist). The remaining gap is a *real*
cross-service run with a live ``claude`` — live processes, not a pure unit — so it
stays staged here and skipped until run by hand.

Runbook (the seamless drop-in loop)
-----------------------------------
1. Install the new/changed services into the env (once):
       bash awm/services/workspace/install.sh

2. Bring up an ISOLATED dev hub (never prod :7819) with the orchestrator +
   agents + workspace services, following the modular isolated harness pattern
   (memory: awm_modular_isolated_test_harness / dag_live_e2e_harness):
       - boot the modular gateway from the worktree via PYTHONPATH dist-roots
       - AWM_WORKSPACE=/tmp/awm-dropin-e2e, a spare AWM_PORT (e.g. 7821)
       - the gateway filesystem-discovers awm/services/{orchestrator,agents,workspace}
       - a real project exists (orch_node_open validates via scopes project_search)

3. Drive the loop by POSTing to the gateway /invoke endpoint
   (memory: awm_invoke_args_key — params go under "args", NOT "arguments"):

   a. orch_node_open {project, goal, repo?: {name, project, scope}}
      → asserts: returns {task_id, workspace_slug, agent_ref}; the node appears in
        orch_dag wired as a prerequisite of root; a WORKER (claude harness) is
        placed in tasks/<workspace_slug>/ with CLAUDE.md + .awm/data + (if repo)
        repos/<name> symlinked to the existing scope; the task is attached=1.

   b. ATTACH: open the agents 'transcript' WS session {scope: workspace_slug}.
      → asserts: orch_dag shows attached=1 (transcript presence → set_attached).

   c. ADMIN WHILE ATTACHED: invoke relocate_self {} as the placed agent's identity
      (X-Awm-As: <workspace_slug>).
      → asserts: ok; orch_dag shows the funnel re-pointed (still reaches root).

   d. DETACH: close the transcript WS, then invoke relocate_self again.
      → asserts: rejected ("attached-only command — no human is attached").

   e. DELIVER: the worker edit_deliverable(<synthetic contract>) + indicate_done;
      the supervisor stop-boundary fires orch.deliver.
      → asserts: the node completes and root completes.

4. Tear down the isolated hub (reap orphans by AWM_HUB_URL origin; memory:
   awm_gateway_service_lifecycle / services reap).

Un-skip the test below (or convert this runbook into a driver script) once the
dev hub is standing.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.skip(reason="live dev hub + real claude — run by hand "
                         "(see module docstring runbook)")
async def test_live_hub_dropin_loop():
    # Placeholder: the runbook above is the executable spec. Implement against a
    # standing dev hub (:7821) with the orchestrator + agents + workspace
    # services and a real claude.
    raise NotImplementedError
