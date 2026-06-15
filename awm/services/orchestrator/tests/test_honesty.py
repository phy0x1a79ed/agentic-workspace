"""The manifest-omission honesty mechanism.

The four privileged plan-mutation ops must be absent from the MCP surface
(``API_MANIFEST["functions"]`` — what ``catalog.list_tools`` projects) yet remain
reachable by the agents harness through the gateway's catch-all dispatch, which
resolves against the control channel's ``HANDLERS`` dict, not the manifest. We
exercise the *exact* adapter-side dispatch path the gateway uses
(``ServiceAdapter._dispatch``) to prove a manifest-omitted handler still runs.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

PUBLIC = {"orch_task_attach", "orch_status", "orch_frontier", "orch_dag"}
PRIVILEGED = {"claim", "deliver", "fail", "decompose_commit"}


def test_manifest_lists_only_public_ops(orch):
    from awm.orchestrator import hub_adapter

    tool_names = {f.get("tool") or f["name"]
                  for f in hub_adapter.API_MANIFEST["functions"]}
    handler_names = set(hub_adapter.HANDLERS)

    assert tool_names == PUBLIC
    # Privileged ops are wired as handlers but never appear in the manifest.
    assert PRIVILEGED <= handler_names
    manifest_fn_names = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
    assert manifest_fn_names.isdisjoint(PRIVILEGED)


async def test_privileged_op_is_dispatchable_though_unmanifested(orch):
    """A privileged op, absent from the manifest, still routes through the
    adapter's dispatch (the same resolution the gateway's /svc/fn uses) and
    mutates the plan."""
    from awm.gatewayclient import ServiceAdapter
    from awm.orchestrator import hub_adapter

    # Attach a doable task (born active) so a privileged `deliver` has something
    # to act on.
    res = orch.operations.orch_task_attach(
        {"project": "p", "goal": "ship it",
         "produces": [{"name": "c1", "spec": "the deliverable"}]})
    tid = res["task_id"]
    task = orch.DAO().get_task(tid)
    assert task["state"] == "active"  # dispatched at attach

    adapter = ServiceAdapter(
        "orchestrator", hub_adapter.API_MANIFEST, hub_adapter.HANDLERS)

    # `deliver` is NOT in the manifest, but _dispatch resolves it from HANDLERS.
    reply = await adapter._dispatch(
        "deliver",
        {"task_id": tid, "agent_ref": task["agent_ref"],
         "contract": "c1", "payload_ref": "artifact:final"},
        None,
    )
    assert reply["ok"] is True
    assert orch.DAO().get_task(tid)["state"] == "completed"


async def test_unknown_function_raises(orch):
    from awm.gatewayclient import ServiceAdapter
    from awm.orchestrator import hub_adapter

    adapter = ServiceAdapter(
        "orchestrator", hub_adapter.API_MANIFEST, hub_adapter.HANDLERS)
    with pytest.raises(RuntimeError):
        await adapter._dispatch("not_a_real_op", {}, None)
