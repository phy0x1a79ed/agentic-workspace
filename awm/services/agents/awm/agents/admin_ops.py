"""Attach-gated DAG-restructuring admin ops — a CONFIGURABLE registry.

A placed agent is an autonomous worker; while a **human is attached** it gains a
set of admin commands to restructure the live DAG (relocate itself, spin up new
nodes, add dependencies, link repos). Those commands are FORBIDDEN when no human
is attached — restructuring the graph is the human's prerogative, not the
autonomous loop's.

This list is the **single source of truth**: editing one entry adds / removes /
renames an admin command everywhere at once, with no manifest + handler + gate
triplication. It drives —

  * the MCP tool entries the agents manifest exposes (``hub_adapter``),
  * the ``HANDLERS`` dispatch (one generic gated relay per op),
  * the per-mode tool profile (which ops an attended worker may call), and
  * the uniform attach-gate (``placement.relay_admin`` reads ``requires_attached``).

Each op resolves the caller's task from its placement identity (the per-placement
``AWM_AS`` → ``X-Awm-As`` → ``as_``) — so the model never passes a token or id —
then forwards to the named orchestrator op as the RESOLVED ``{task_id,
agent_ref}`` (plus the caller's ``project`` when ``inject_project`` is set, e.g.
``create_node`` mints in the caller's project). ``forward`` is the subset of the
caller's args passed straight through.
"""

from __future__ import annotations

ADMIN_OPS: list[dict] = [
    {
        "name": "relocate_self",
        "orch_op": "relocate_task",
        "requires_attached": True,
        "description": (
            "Re-point THIS node's funnel onto a different consumer (the global "
            "root by default), keeping it on a path to root. Only while a human "
            "is attached."
        ),
        "params": [
            {"name": "new_consumer_task", "type": "string", "required": False},
        ],
        "forward": ["new_consumer_task"],
    },
    {
        "name": "create_node",
        "orch_op": "orch_task_attach",
        "requires_attached": True,
        "inject_project": True,
        "description": (
            "Create a new node upstream of a consumer (root by default) — a "
            "prerequisite that runs autonomously. Optionally link a repo. Only "
            "while a human is attached."
        ),
        "params": [
            {"name": "goal", "type": "string", "required": True},
            {"name": "consumer", "type": "string", "required": False},
            {"name": "repo", "type": "object", "required": False},
        ],
        "forward": ["goal", "consumer", "repo"],
    },
    {
        "name": "depend_on",
        "orch_op": "node_add_dependency",
        "requires_attached": True,
        "description": (
            "Add a dependency: make THIS node consume an existing contract (by "
            "id or name). Acyclicity is enforced. Only while a human is attached."
        ),
        "params": [
            {"name": "contract", "type": "string", "required": True},
        ],
        "forward": ["contract", "contract_id"],
    },
    {
        "name": "link_repo",
        "orch_op": "link_repo",
        "requires_attached": True,
        "description": (
            "Link existing scope(s) into THIS node's workspace unit now (cwd "
            "repos/<name>). Only while a human is attached."
        ),
        "params": [
            {"name": "repo", "type": "object", "required": True},
        ],
        "forward": ["repo"],
    },
]

# name → entry, for the relay + tool-profile lookups.
BY_NAME: dict[str, dict] = {op["name"]: op for op in ADMIN_OPS}

# The admin tool NAMES, in registry order — the attended-worker tool profile is
# derived from this (placement.TOOL_PROFILES) and the manifest lists them.
ADMIN_TOOL_NAMES: list[str] = [op["name"] for op in ADMIN_OPS]
