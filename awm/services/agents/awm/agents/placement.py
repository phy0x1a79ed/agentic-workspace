"""Task-bounded placement — the runtime side of the one-DAG orchestrator.

A **placement** drops one agent onto one orchestrator task in a dedicated
**workspace unit** (the DAG execution sandbox: a workdir with the brief, the
read-only input pre-readings, and the deliverable staging — provisioned by the
``workspace`` service, NOT a git scope). The placement record IS the task-bound
``agent_instances`` row (``mode`` != ``'conversational'``); ``placement_token``
names it, ``agent_ref`` is the stable placement identity, ``task_ref`` binds it
to the task. The unit is **one-per-task, reused across the lifecycle**: the same
slug carries a task through PLANNING → VERIFYING_PLAN → ACTIVE, each stage a
fresh agent in a different MODE with a different tool profile.

Four placement modes, one uniform pattern (craft a deliverable, then a
harness-checked stop boundary transitions the task):

- ``plan``   (PLANNING)        — read-only; stages the reserved ``"plan"``
                                  deliverable (objective → a plan).
- ``verify`` (VERIFYING_PLAN)   — NO filesystem; judges the staged plan against
                                  the objective; terminal via approve/reject.
- ``worker`` (ACTIVE)           — full fs; stages each ``contracts_out``
                                  deliverable, then ``indicate_done``.
- ``planner`` (DECOMPOSING)     — read-only; buffers a sub-DAG via graph tools,
                                  committed at accept (funnel-checked).

This module owns ``place_on_task`` (contract A), the worker/verifier/planner
tool relays (contract D → contract B), the steering handshake (durable attach +
mutual-detach consent + the objective record), and the supervision driver
``on_turn_boundary`` (per-mode acceptance + hard turn budget). Durable
``attached`` is the single source of truth (viewing a transcript never attaches);
an attached OR paused node freezes the supervisor. Detach is a two-consent
handshake — the agent's ``request_detach`` (payload = the refreshed objective
record) + the user's ``set_user_done`` — that fires automatically when both
agree. Everything reuses ``agent_instances`` (spawn / transcript / stdin) rather
than forking it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import awm.gatewayclient as gatewayclient
from awm.agents import admin_ops
from awm.agents import agent_instances as ai
from awm.agents import orch_client
from awm.agents.agent_instances import TASK_TURN_BUDGET, TASK_WARN_REMAINING

log = logging.getLogger("awm.agents.placement")

# Explicit per-harness model defaults. A placement's model is HARD-REQUIRED at
# the spawn boundary (``agent_instances.create_session`` raises on a blank model
# so a claude TUI can never omit ``--model`` and silently inherit the operator's
# ambient ``ANTHROPIC_MODEL``). ``_resolve_driver`` therefore resolves a blank
# model to one of these concrete ids — keyed on the *final* harness — rather than
# passing ``None`` through. ``claude`` matches the orchestrator's dispatch pin
# (``DEFAULT_CLAUDE_PLACEMENT_MODEL``); ``opencode`` matches the opencode
# backend's own free-Zen default (``AGENTCORE_OPENCODE_MODEL``).
DEFAULT_CLAUDE_PLACEMENT_MODEL = "haiku"
DEFAULT_OPENCODE_PLACEMENT_MODEL = "deepseek-v4-flash-free"


# ---------------------------------------------------------------------------
# Long-session hardening tunables (T5) — read at use so a redeploy env change
# applies with no restart.
# ---------------------------------------------------------------------------

# Stall watchdog: a placement silent (no harness event) for longer than this many
# seconds is failed TYPED transient and re-placed. Default 30 min. KNOWN
# LIMITATION: one long tool call emits NO events between tool-use and tool-result
# (the harness is busy, not stalled), so the window must be generous enough to
# clear the longest legitimate single tool call — the 30-min default is that
# mitigation. Shrink it (e.g. for a live drill) via the env var.
_DEFAULT_STALL_S = 1800.0

# Auto-compact: a claude placement whose context ratio (used/max) is at or past
# this fraction gets a native ``/compact`` injected at the turn boundary.
_DEFAULT_COMPACT_THRESHOLD = 0.8

# Auto-compact cooldown — no re-fire within this many seconds of a ``/compact``
# (the ratio only drops after the compact completes, which takes a turn or two).
_COMPACT_COOLDOWN_S = 300.0

# Stall-watchdog sweep cadence.
_STALL_SWEEP_S = 60.0


def _stall_threshold_s() -> float:
    try:
        return float(os.environ.get("AWM_PLACEMENT_STALL_S") or _DEFAULT_STALL_S)
    except (TypeError, ValueError):
        return _DEFAULT_STALL_S


def _compact_threshold() -> float:
    try:
        return float(os.environ.get("AWM_COMPACT_THRESHOLD")
                     or _DEFAULT_COMPACT_THRESHOLD)
    except (TypeError, ValueError):
        return _DEFAULT_COMPACT_THRESHOLD


# ---------------------------------------------------------------------------
# Identity minting
# ---------------------------------------------------------------------------

def _mint_agent_ref() -> str:
    return "agt-" + uuid.uuid4().hex


def _mint_placement_token() -> str:
    return "plt-" + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Per-mode tool profiles (contract D) — split across two levers
# ---------------------------------------------------------------------------
# The whole placement toolset now collapses onto ONE generic MCP domain tool,
# ``mcp__awm__agent`` (called with ``{verb, args}``), so claude's
# ``--allowedTools`` lever can no longer scope WHICH placement verb runs — it is
# all-or-nothing on the domain tool, and opencode (the default harness) ignores
# the lever entirely. So the levers split:
#
# * ``TOOL_PROFILES`` (claude --allowedTools) still scopes FILESYSTEM access via
#   the built-in tools (verify = none, plan/planner = read-only, worker = full)
#   and grants the single ``mcp__awm__agent`` domain tool.
# * ``VERB_PROFILES`` is the per-mode allowlist of agent-domain VERBS, enforced
#   SERVER-SIDE in :func:`ensure_verb_allowed` (so it holds under opencode too).

_BUILTIN_READONLY = ["Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite"]
_BUILTIN_FULL = _BUILTIN_READONLY + ["Write", "Edit", "Bash", "NotebookEdit"]
# The accept verifier's fs profile: read + RUN (Bash) so it can execute the
# machine-checkable acceptance checks, but explicitly NO Write/Edit — it verifies
# the worker's claimed delivery, it never fixes it.
_BUILTIN_VERIFY_EXEC = _BUILTIN_READONLY + ["Bash"]

_AGENT_DOMAIN = "mcp__awm__agent"

TOOL_PROFILES: dict[str, list[str]] = {
    "worker": _BUILTIN_FULL + [_AGENT_DOMAIN],
    "plan": _BUILTIN_READONLY + [_AGENT_DOMAIN],
    "planner": _BUILTIN_READONLY + [_AGENT_DOMAIN],
    "verify": [_AGENT_DOMAIN],  # no filesystem at all
    "accept": _BUILTIN_VERIFY_EXEC + [_AGENT_DOMAIN],  # read + run, NO edit
}

# Claude Code's internal plan mode is DISABLED for every placement — the DAG's
# own plan stage IS the plan mode (a placed agent must never enter CC plan mode).
# ``TOOL_PROFILES`` are already strict allow-whitelists that exclude these tools,
# so this ``--disallowedTools`` deny is belt-and-suspenders: it holds even if a
# profile widens later and documents the intent explicitly on the spawn argv.
# Applied to EVERY mode (the deny is mode-independent). opencode ignores the
# lever, but it has no plan mode to enter, so the guarantee is claude-side.
_PLAN_MODE_TOOLS = ["EnterPlanMode", "ExitPlanMode"]

# Attach-gated DAG-restructuring verbs (configurable registry). PERMITTED for
# every worker, but ``relay_admin`` rejects each unless a human is attached — so
# an unattended worker may name them yet can never restructure the DAG.
_ADMIN_VERBS = set(admin_ops.ADMIN_TOOL_NAMES)
# Steering-handshake verbs a placed agent drives (contract D). ``request_detach``
# / ``rescind_detach`` are attached-only (checked in the relay); ``request_steering``
# is the mid-run wants-a-human flag, valid while detached. verify stays
# verdict-only, so it does NOT get them.
_STEERING_VERBS = {"request_detach", "rescind_detach", "request_steering"}
_WORKER_VERBS = {"edit_deliverable", "indicate_done", "task_fail"} | _STEERING_VERBS
_PLANNER_VERBS = {
    "add_subtask", "add_dependency", "define_contract",
    "search_tasks", "search_contracts", "indicate_done", "task_fail",
} | _STEERING_VERBS
_VERIFY_VERBS = {"approve_plan", "reject_plan", "task_fail"}
# The accept verifier's terminal verdict verbs. Like verify it is verdict-only
# (no steering, no admin, no deliverable staging) — it accepts or rejects the
# worker's claimed delivery, or bails via task_fail.
_ACCEPT_VERBS = {"accept_work", "reject_work", "task_fail"}
# ``set_title`` is the label-derivation seam a steered agent uses at detach time
# (still attach-gated in ``relay_admin``). worker gets it via ``_ADMIN_VERBS``;
# plan/planner nodes can also be attended (attach-on-rest places the next leg
# attended), so they carry it too — so the shared steering instruction to set a
# title holds in every steerable mode.
_STEER_ADMIN_VERBS = {"set_title"}

VERB_PROFILES: dict[str, set[str]] = {
    "worker": _WORKER_VERBS | _ADMIN_VERBS,
    "plan": _WORKER_VERBS | _STEER_ADMIN_VERBS,
    "planner": _PLANNER_VERBS | _STEER_ADMIN_VERBS,
    "verify": set(_VERIFY_VERBS),
    "accept": set(_ACCEPT_VERBS),
}

# task_fail is the universal escape hatch — always permitted regardless of mode.
_ALWAYS_ALLOWED_VERBS = {"task_fail"}

_VALID_MODES = set(TOOL_PROFILES)


def _allowed_tools_for(mode: str) -> list[str]:
    return list(TOOL_PROFILES.get(mode, TOOL_PROFILES["worker"]))


def _disallowed_tools_for(mode: str) -> list[str]:
    """The explicit ``--disallowedTools`` deny for a placement (all modes).

    Denies Claude Code's internal plan-mode tools so a placed agent can never
    enter CC plan mode — the DAG's plan stage is the only plan mode."""
    return list(_PLAN_MODE_TOOLS)


def _allowed_verbs_for(mode: str) -> list[str]:
    return sorted(VERB_PROFILES.get(mode, VERB_PROFILES["worker"]))


def ensure_verb_allowed(as_: str | None, verb: str) -> None:
    """Server-side gate: reject an agent-domain verb the caller's placement mode
    does not permit. Raises :class:`PlacementError` (→ a structured 500 the MCP
    proxy passes to the model verbatim).

    This is the per-verb confinement for placed agents now that the toolset is
    one opaque ``mcp__awm__agent`` domain tool — and the ONLY enforcement under
    the default opencode harness, which ignores ``--allowedTools``. It is
    defense-in-depth / a guardrail, NOT a trust boundary: the ``as_`` identity is
    plaintext + spoofable on the unauthenticated loopback bus (an agent with
    ``Bash`` can curl ``/invoke`` directly). ``task_fail`` is always allowed."""
    if verb in _ALWAYS_ALLOWED_VERBS:
        return
    row = _resolve_open_placement_for(as_)
    allowed = _row_data(row).get("allowed_verbs")
    if allowed is None:
        # Legacy row (spawned before allowed_verbs was recorded) — fall back to
        # the static per-mode profile so the gate is never fail-open.
        allowed = _allowed_verbs_for(_spec(row).get("mode") or "worker")
    if verb not in allowed:
        raise PlacementError(
            f"verb {verb!r} is not permitted for this placement "
            f"(allowed: {', '.join(sorted(allowed))})")


# ---------------------------------------------------------------------------
# Brief / kickoff rendering
# ---------------------------------------------------------------------------

def _fmt_list(label: str, items: list) -> str:
    if not items:
        return f"{label}: (none)"
    return label + ":\n" + "\n".join(f"  - {i}" for i in items)


def _finish_worker(contracts_out: list) -> str:
    return (
        "## How to finish\n\n"
        "Craft each required output by calling "
        "`edit_deliverable(contract, content)` — call it as "
        "many times as you need (each call REPLACES the staged content for that "
        "contract). When every output is staged and correct, call "
        "`indicate_done()`. Editing a deliverable after "
        "indicating done clears the done flag — re-indicate.\n\n"
        "When you stop with done set and every output staged & valid, the task "
        "advances automatically. If you cannot complete it, call "
        "`task_fail(reason_type, reason_text, partial_ref?)`.\n\n"
        f"Outputs you must stage: {', '.join(contracts_out) or '(none)'}\n"
        "_Your placement tools act on your own task automatically — you never "
        "pass a token or id._\n"
    )


def _finish_plan() -> str:
    """The ``plan``-mode "how to finish": produce a Claude-Code-planning-prompt
    plan document and stage it as the reserved ``plan`` deliverable.

    The MECHANICAL contract is unchanged (stage ``plan`` via ``edit_deliverable``
    → ``indicate_done`` → the kernel's plan_delivered → verifying_plan leg); only
    the instructed document structure + quality bar are new. The DAG's plan stage
    IS the plan mode — there is no CC internal plan mode to fall back on."""
    return (
        "## How to finish\n\n"
        "Produce a PLAN DOCUMENT and stage it as the single `plan` deliverable: "
        "call `edit_deliverable(contract=\"plan\", content=\"<the plan "
        "document>\")` — call it as many times as you need (each call REPLACES "
        "the staged plan) — then `indicate_done()` when the plan is complete and "
        "correct. Editing after indicating done clears the flag — re-indicate.\n\n"
        "When you stop with done set and the plan staged, the task advances to "
        "verification automatically. If you cannot produce a plan, call "
        "`task_fail(reason_type, reason_text, partial_ref?)`.\n\n"
        "### Plan document structure\n\n"
        "Write the plan as markdown with these sections, in this order:\n"
        "- **Context** — what prompted this task and the intended outcome, in "
        "plain language.\n"
        "- **Objective record** — the ratified objective record above, quoted "
        "VERBATIM as the alignment anchor (if none was ratified, restate the "
        "objective from the goal).\n"
        "- **Goals** — numbered, plain-language statements of intent (no file "
        "paths or identifiers).\n"
        "- **Acceptance criteria** — observable, testable conditions that mark "
        "the task done.\n"
        "- **Task breakdown** — the child decomposition; one line per child "
        "describing what that child ships.\n"
        "- **Approach per task** — a short subsection per child naming the shape "
        "of its work, each ending with a **Gotchas:** line of failure modes.\n\n"
        "Keep it tight and checkable — this plan is verified against the "
        "objective record before any work begins.\n"
        "_Your placement tools act on your own task automatically — you never "
        "pass a token or id._\n"
    )


def _steering_section() -> str:
    """The "how to finish" steering contract, appended to worker/plan/planner
    briefs. Teaches both sides of the handshake: align-then-detach while a human
    is attached, and flag-for-steering while running detached."""
    return (
        "## Steering — how to finish alignment\n\n"
        "**While a human is attached, your job is to ALIGN, not to rush into "
        "work.** Distill their intent — the objective, the constraints, what "
        "*done* looks like, and the non-goals — into a concise objective record "
        "(markdown), and when you believe you understand and are ready to work "
        "call `request_detach(objective=\"<the record>\")`: writing the record and "
        "consenting to detach are ONE act. Detach is by mutual consent — the "
        "human sets their own 'done' bit asynchronously, and the moment both "
        "agree the node detaches automatically and you proceed autonomously (no "
        "acknowledgement step).\n\n"
        "As part of requesting detach, also set a short human-readable label with "
        "`set_title(title=\"...\")` — ≤8 words, imperative, derived from the "
        "objective (e.g. \"Migrate auth to OAuth2\"). This is the node's headline "
        "in the task view; you may set it only while attached.\n\n"
        "If the user signals they are done but you are still unsure, ask precise "
        "questions and **stay** — do NOT request detach to be polite. A new user "
        "message after you have requested detach clears your readiness; "
        "re-request once you have re-confirmed. Either side may `rescind_detach()` "
        "while attached.\n\n"
        "**When you are running detached** and hit something that genuinely needs "
        "human judgment, call `request_steering()` and carry on with your best "
        "judgment (or park that thread) — it flags a human without freezing you.\n"
    )


def _finish_planner() -> str:
    return (
        "## How to finish\n\n"
        "This task is too large to do directly — decompose it into a sub-DAG. "
        "Build the graph with `add_subtask(id, objective, "
        "contracts_out?, title?)`, `add_dependency(from_id, to_id, "
        "contract?)`, and `define_contract(name, ...)`. Give "
        "each subtask a `contracts_out` (what it produces) and a short `title` "
        "(≤8 words, imperative, derived from its objective — the child's headline "
        "in the task view); a dependency edge "
        "means `to_id` consumes a contract that `from_id` produces (name it via "
        "`contract` when `from_id` produces more than one). Use "
        "`search_tasks` / `search_contracts` to reuse existing nodes. **Every "
        "subtask must funnel into this task** (the sub-DAG has a single sink and "
        "every node reaches it). When the graph is complete, call "
        "`indicate_done()` to commit it. _Your placement tools act on your own "
        "task automatically — you never pass a token or id._\n"
    )


def _fmt_checks(checks: list) -> str:
    """Render a spec's checks as verbatim markdown bullets (name → cmd → expect)."""
    rendered = []
    for c in checks or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or "(unnamed)"
        cmd = c.get("cmd") or ""
        exp_exit = c.get("expect_exit", 0)
        detail = f"  - **{name}**: `{cmd}` (expect exit {exp_exit}"
        exp_out = c.get("expect_output")
        if exp_out:
            detail += f", output containing {exp_out!r}"
        detail += ")"
        rendered.append(detail)
    return "\n".join(rendered)


def _finish_accept(spec: dict | None) -> str:
    """The ``accept``-mode "how to finish": run each machine check from the
    infra-owned ``accept_spec`` and reach a verdict — the verifier CANNOT edit or
    fix the work, only verify it.

    The verifier has READ + Bash-EXECUTE (no Write/Edit): it runs each ``cmd``,
    compares exit/output, and calls ``accept_work`` ONLY if every check passes AND
    the objective is met, else ``reject_work`` naming the failed check."""
    spec = spec or {}
    objective = str(spec.get("objective") or "").strip()
    checks = spec.get("checks") or []
    parts = [
        "## How to finish (acceptance verification)\n\n"
        "You are an **independent acceptance verifier**. The worker has CLAIMED a "
        "delivery — nothing is accepted, and no downstream task advances, until "
        "you verify it. You have READ and Bash-EXECUTE access **but you CANNOT "
        "edit or fix anything** (no Write/Edit): your only job is to VERIFY the "
        "claim, then accept or reject it.\n",
    ]
    if objective:
        parts.append(
            "### Objective — the bar the work must meet\n\n"
            f"{objective}\n")
    checks_md = _fmt_checks(checks)
    if checks_md:
        parts.append(
            "### Acceptance checks — run EACH one via Bash\n\n"
            f"{checks_md}\n")
    else:
        parts.append(
            "### Acceptance checks\n\n(no machine checks were specified — judge "
            "the objective directly against what the worker produced)\n")
    parts.append(
        "Run every check above with `Bash`. The linked repos are under "
        "`repos/<name>` and the worker's claimed artifacts are materialized "
        "read-only under `./inputs/`. Then reach exactly one verdict:\n\n"
        "- **Accept** — ONLY if every check passes AND the objective is met: call "
        "`accept_work(evidence=\"<what you ran and observed>\")`. This promotes "
        "the claim into a real delivery and completes the task.\n"
        "- **Reject** — if any check fails or the objective is not met: call "
        "`reject_work(reason=\"<name the failed check(s) and what went wrong>\")`. "
        "The worker loops back to fix it under a bounded budget.\n\n"
        "You CANNOT edit or fix the work yourself — you only verify. If you "
        "cannot run the checks at all, call `task_fail(reason_type, reason_text)`."
        "\n_Your placement tools act on your own task automatically — you never "
        "pass a token or id._\n")
    return "\n".join(parts)


def _acceptance_criteria_section(spec: dict | None) -> str:
    """Worker-brief section quoting the infra-owned ``accept_spec`` verbatim.

    Titled EXACTLY "## Acceptance criteria (authoritative — you may NOT change or
    narrow these)" — an independent verifier will check the delivery against these
    before it is accepted, and they are fixed (non-narrowable)."""
    spec = spec or {}
    objective = str(spec.get("objective") or "").strip()
    checks = spec.get("checks") or []
    parts = [
        "## Acceptance criteria (authoritative — you may NOT change or narrow "
        "these)\n\n"
        "An INDEPENDENT verifier will check your delivery against the following "
        "before it is accepted. These are fixed by the task owner — you cannot "
        "renegotiate, weaken, or narrow them. Meet them exactly.\n",
    ]
    if objective:
        parts.append(f"**Objective:** {objective}\n")
    checks_md = _fmt_checks(checks)
    if checks_md:
        parts.append("Checks that will be run against your delivery:\n\n"
                     f"{checks_md}\n")
    return "\n".join(parts)


def _rework_section(rework: dict | None) -> str:
    """Worker-brief section rendered when a prior delivery was rejected.

    ``rework`` is ``{reason_text, partial_ref}`` — the verifier's rejection reason
    and (optionally) a ref to the partial work, so the re-dispatched worker knows
    what to fix."""
    rework = rework or {}
    reason = str(rework.get("reason_text") or "").strip()
    partial = rework.get("partial_ref")
    parts = [
        "## Rework — your previous delivery was REJECTED\n\n"
        "A prior attempt at this task was rejected by the acceptance verifier. "
        "Address the feedback below, then re-stage your outputs and indicate done "
        "again.\n",
    ]
    if reason:
        parts.append(f"**Rejection reason:** {reason}\n")
    if partial:
        parts.append(f"**Partial work ref:** {partial}\n")
    return "\n".join(parts)


def render_brief(*, task_id: str, mode: str, brief: str,
                 contracts_in: list, contracts_out: list,
                 prereadings: list, attached: bool = False,
                 objective: str = "",
                 accept_spec: dict | None = None,
                 rework_reason: dict | None = None) -> str:
    """Render a worker/plan/planner unit ``CLAUDE.md`` (the brief on disk —
    auto-loaded by the claude harness).

    ``attached`` seeds the steering "how to finish alignment" section (the mutual
    detach handshake); ``objective`` is the ratified objective record, rendered
    verbatim when non-empty so a re-placement inherits the distilled intent.

    ``verify`` does not use this — it has no filesystem, so its objective+plan
    ride the kickoff stdin instead (see ``_verify_kickoff``)."""
    preread_names = [p.get("name") for p in (prereadings or []) if p.get("name")]
    role = {
        "plan": "plan-worker (read-only): produce a PLAN to accomplish the "
                "objective; you may inspect the repo but must not modify it",
        "planner": "planner: decompose this task into a sub-DAG",
        "worker": "worker: do the task and produce its outputs",
        "accept": "accept verifier (read + execute, NO edit): independently verify "
                  "the worker's claimed delivery against machine-checkable "
                  "acceptance criteria — accept it or reject it, never fix it",
    }.get(mode, "worker")

    parts = [
        f"# Task placement: {task_id}\n",
        f"You are a **{mode}** on orchestrator task `{task_id}`.\n_{role}._\n",
        f"## Objective\n\n{brief or '(no brief provided)'}\n",
    ]
    if (objective or "").strip():
        parts.append(
            "## Objective record (ratified)\n\nThe distilled intent for this "
            "node — treat it as the alignment anchor. If a prior steering session "
            "ended unexpectedly, the conversation transcript is preserved and this "
            f"record stands:\n\n{objective.strip()}\n")
    if preread_names:
        parts.append(
            "## Inputs (pre-readings)\n\nMaterialized read-only under "
            f"`./inputs/`: {', '.join(preread_names)}\n")
    parts.append(
        "## Contracts\n\n"
        f"{_fmt_list('Inputs (contracts_in)', contracts_in)}\n\n"
        f"{_fmt_list('Outputs (contracts_out)', contracts_out)}\n")
    if mode == "planner":
        parts.append(_finish_planner())
    elif mode == "plan":
        parts.append(_finish_plan())
    elif mode == "accept":
        parts.append(_finish_accept(accept_spec))
    else:
        # worker: when the task is gated, quote the authoritative acceptance
        # criteria verbatim, and render the rework feedback if a prior delivery
        # was rejected — both BEFORE the mechanical how-to-finish.
        if accept_spec:
            parts.append(_acceptance_criteria_section(accept_spec))
        if rework_reason:
            parts.append(_rework_section(rework_reason))
        parts.append(_finish_worker(contracts_out))
    # Steering (the attach/detach handshake) applies only to the human-steerable
    # modes. The accept verifier (like verify) is never attended, so it gets no
    # steering section.
    if mode in ("worker", "plan", "planner"):
        parts.append(_steering_section())
    parts.append(
        f"\n_When running detached you are unattended: idle turns count against a "
        f"hard {TASK_TURN_BUDGET}-turn budget (an attached or paused node is "
        f"frozen and never force-failed)._\n")
    return "\n".join(parts)


def _placement_tool_note() -> str:
    """One-liner on the collapsed tool surface: every placement verb is reached
    through the single ``agent`` MCP domain tool (``agent(verb, args)``); call
    ``agent(verb="describe")`` to see the verbs your mode permits."""
    return (
        "Your placement verbs (edit_deliverable, indicate_done, task_fail, …) are "
        "all reached through the single `agent` tool — call it as "
        "`agent(verb=\"<name>\", args={...})`; `agent(verb=\"describe\")` lists "
        "every verb with its parameters."
    )


def _brief_objective(brief: str) -> str:
    """Extract the ratified objective record from a structured (JSON) brief.

    The orchestrator threads the durable ``objective`` onto the brief dict when
    non-empty; a plain (non-JSON) brief carries none. Returns ``""`` when absent."""
    if not brief:
        return ""
    try:
        data = json.loads(brief)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict):
        return str(data.get("objective") or "")
    return ""


def _brief_goal(brief: str) -> str:
    """Extract the create-time goal from a structured (JSON) brief.

    The verify fallback anchor when no objective record was ratified. A plain
    (non-JSON) brief is treated as the goal verbatim."""
    if not brief:
        return ""
    try:
        data = json.loads(brief)
    except (ValueError, TypeError):
        return brief
    if isinstance(data, dict):
        return str(data.get("goal") or "")
    return ""


def _brief_accept_spec(brief: str) -> dict | None:
    """Extract the infra-owned ``accept_spec`` from a structured (JSON) brief.

    The orchestrator threads the parsed spec (``{objective, checks}``) onto the
    brief for a gated worker AND for the accept verifier. Returns ``None`` when
    absent (a NULL accept_spec ⇒ the legacy un-gated path)."""
    if not brief:
        return None
    try:
        data = json.loads(brief)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        spec = data.get("accept_spec")
        if isinstance(spec, dict):
            return spec
    return None


def _brief_rework_reason(brief: str) -> dict | None:
    """Extract the ``rework_reason`` (``{reason_text, partial_ref}``) threaded onto
    a re-dispatched worker's brief after a ``reject_work``. ``None`` when absent."""
    if not brief:
        return None
    try:
        data = json.loads(brief)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        rr = data.get("rework_reason")
        if isinstance(rr, dict):
            return rr
    return None


def _kickoff_text(*, task_id: str, mode: str) -> str:
    """First stdin message for a worker/plan/planner — wakes the turn loop."""
    return (
        f"You have been placed on task `{task_id}` as a **{mode}**. Read "
        f"`./CLAUDE.md` for your objective, inputs, contracts, and how to "
        f"finish. Your placement tools act on this task automatically — you "
        f"never pass a token or id. {_placement_tool_note()} Begin now."
    )


def _verify_kickoff(*, task_id: str, objective: str, goal: str,
                    contracts_out: list, plan_text: str) -> str:
    """The verifier's entire context — it has NO filesystem, so the ratified
    objective record and the proposed plan ride stdin, not a file read.

    The verdict is plan-vs-ratified-intent: the objective record is the anchor
    when set, else the create-time goal. Both come off the brief the orchestrator
    threads (``objective`` for every mode + ``goal``)."""
    anchor = (objective or "").strip()
    if anchor:
        anchor_kind = "objective record (ratified)"
        anchor_text = anchor
    else:
        anchor_kind = "goal (no objective record was ratified)"
        anchor_text = (goal or "").strip() or "(no objective provided)"
    return (
        f"You are a PLAN VERIFIER for task `{task_id}`. You have NO filesystem "
        "access — judge only from the text below.\n\n"
        f"## Objective — the {anchor_kind}\n\nThis is the ratified intent this "
        "plan must satisfy. Judge the plan against THIS, verbatim:\n\n"
        f"{anchor_text}\n\n"
        f"## Required outputs\n\n{', '.join(contracts_out) or '(none)'}\n\n"
        f"## Proposed plan\n\n{plan_text or '(no plan was staged)'}\n\n"
        "## Your job\n\nYour verdict is exactly: does this plan satisfy the "
        "objective above? Approve only if the plan covers the objective's intent "
        "and its acceptance criteria. Reject if the plan misses acceptance "
        "coverage, or shows SCOPE CREEP — deliverables or work beyond what the "
        "objective requires. This is a sanity check against the ratified intent, "
        "not a re-plan. Call exactly one: `approve_plan()` if it satisfies the "
        "objective, or `reject_plan(reason=\"...\")` with a concise reason if it "
        "does not. (These act on your own task automatically — no token or id.)"
        "\n\n" + _placement_tool_note()
    )


# ---------------------------------------------------------------------------
# Workspace unit provisioning (contract C — the workspace service)
# ---------------------------------------------------------------------------

async def _workspace_create(unit_slug: str, context_md: str,
                            prereadings: list, repos: list | None = None) -> str:
    """Provision (or re-activate) the unit; return its on-disk path.

    ``repos`` (``[{name, project, scope}]`` — each item a scope's own
    scopes-service coordinates) links existing scopes into the unit under
    ``repos/<name>`` — threaded straight through to the workspace service, which
    builds the symlinks (this service makes no scopes call of its own)."""
    res = await gatewayclient.call("workspace", "workspace_create", {
        "unit_slug": unit_slug,
        "context_md": context_md, "prereadings": prereadings or [],
        "repos": repos or [],
    })
    return (res or {}).get("path") or ""


async def _retain_unit(scope: str) -> None:
    """Free the unit but KEEP its contents (the lifecycle-reuse / audit state).

    The ``scope_complete(cleanup=False)`` analog. Best-effort: a transient RPC
    failure is logged, not raised (the orchestrator already has the ack)."""
    try:
        await gatewayclient.call("workspace", "workspace_retain", {
            "unit_slug": scope,
        })
    except Exception:  # noqa: BLE001
        log.warning("workspace_retain failed for %s", scope, exc_info=True)


async def _await_scope_free(scope: str,
                            *, timeout: float = 20.0, grace: float = 2.0) -> None:
    """Wait for any prior-stage placement subprocess on this unit to vacate.

    The lifecycle reuses ONE unit across plan → verify → worker: each stage is a
    fresh agent on the SAME ``scope`` (the unit slug). The previous stage acks
    its terminal B-op (``deliver``/``approve_plan``) BEFORE its subprocess is
    retired (retire is async), so when the orchestrator dispatches the next leg
    it can arrive while the old subprocess still registers the scope — and
    ``create_session`` would reject it (``ScopeBusyError``). Poll until the scope
    is free; if a (closed-placement) session overstays the grace period, stop it
    and keep waiting. No-op when the scope is already free (the common case)."""
    waited = 0.0
    nudged = False
    while True:
        if ai.get_session_by_scope(scope) is None:
            return
        if waited >= grace and not nudged:
            sess = ai.get_session_by_scope(scope)
            if sess is not None:
                try:
                    await ai.stop_session(sess.id)  # push the lingering retire
                except Exception:  # noqa: BLE001
                    pass
            nudged = True
        if waited >= timeout:
            raise PlacementError(
                f"unit {scope} still busy after {timeout}s "
                "(prior placement did not vacate the scope)")
        await asyncio.sleep(0.1)
        waited += 0.1


def _read_staged_plan(workspace_path: str) -> str:
    """Read the plan-stage deliverable (``deliverable/plan/payload``) off disk
    so it can be embedded in the verifier's kickoff (the verifier has no fs)."""
    if not workspace_path:
        return ""
    p = Path(workspace_path) / "deliverable" / "plan" / "payload"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return ""


def _resolve_driver(args: dict) -> tuple[str, str, str | None]:
    """Resolve the ``(harness, model, effort)`` a dispatch spawns under.

    Precedence per field: explicit ``args`` > env override
    (``AWM_PLACEMENT_HARNESS`` / ``AWM_PLACEMENT_MODEL`` / ``AWM_PLACEMENT_EFFORT``)
    > the opt-in ``agents`` config contract's stored default > the contract field
    default (``claude`` / ``haiku`` / ``medium`` — the UNIFORM placement default,
    T5). The contract read is a single-row load, so a change saved in the settings
    UI applies on the next dispatch with no restart, while explicit overrides
    still win. The old attach-state fork (attached → claude, detached → free
    opencode) is gone: EVERY placement defaults the same way.

    The **model is never left blank**: once the final harness is known, a blank
    model resolves to that harness's explicit default (see the module constants)
    instead of ``None``. This makes the model explicit end-to-end — a claude
    placement can never fall through to the operator's ambient ``ANTHROPIC_MODEL``
    (the CLI default) — and upholds ``create_session``'s hard requirement that a
    spawn always carry a concrete model.
    """
    from awm.agents.driver_config import DRIVER_CONTRACT
    driver = DRIVER_CONTRACT.load_model()
    harness = (args.get("harness") or os.environ.get("AWM_PLACEMENT_HARNESS")
               or driver.harness)
    model = (args.get("model") or os.environ.get("AWM_PLACEMENT_MODEL")
             or driver.model)
    if not model:
        model = (DEFAULT_CLAUDE_PLACEMENT_MODEL if harness == "claude"
                 else DEFAULT_OPENCODE_PLACEMENT_MODEL)
    effort = (args.get("effort") or os.environ.get("AWM_PLACEMENT_EFFORT")
              or getattr(driver, "effort", None))
    return harness, model, effort


# ---------------------------------------------------------------------------
# place_on_task (contract A)
# ---------------------------------------------------------------------------

async def place_on_task(args: dict) -> dict:
    """Drop a supervised agent onto a task in its workspace unit.

    Sequence: resolve mode + slug → ``workspace_create`` (unit dir + brief +
    pre-readings) BEFORE spawn → spawn live via the existing ``create_session``
    path (with the per-mode tool profile + the unit as workdir) → record the
    placement spec on the row → kickoff via stdin. Returns
    ``{agent_ref, unit_slug, scope, placement_token}``."""
    task_id = args["task_id"]
    # ``unit_slug`` is the workspace-unit slug AND the agent scope/identity.
    unit_slug = args["unit_slug"]
    brief = args.get("brief", "")
    contracts_in = args.get("contracts_in") or []
    contracts_out = args.get("contracts_out") or []
    prereadings = args.get("prereadings") or []
    repos = args.get("repos") or []
    # Born-attended intent (a human-driven drop-in via ``orch_node_open``):
    # seeds the row's ``attached`` flag so the supervisor freezes from turn one
    # (P2). Transcript WS open/close still flips it live via ``set_attached``.
    attached = bool(args.get("attached"))
    # Sticky human pause (also seeded from ``orch_node_open`` / a redispatch of a
    # paused row): the supervisor freezes on ``attached OR paused``, and unlike
    # ``attached`` it is never cleared on detach. Stored in the instance data so
    # ``on_turn_boundary`` reads it; mirrored durably via the agents ``set_paused``
    # verb.
    paused = bool(args.get("paused"))
    mode = args.get("mode", "worker")
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown placement mode {mode!r}; "
                         f"choices: {sorted(_VALID_MODES)}")
    if mode == "plan" and not contracts_out:
        contracts_out = ["plan"]  # the reserved PLANNING deliverable

    agent_ref = _mint_agent_ref()
    placement_token = _mint_placement_token()

    # The ratified objective record (threaded on the brief by the orchestrator
    # when non-empty) — seeded onto the row + rendered so a re-placement inherits
    # the distilled intent.
    objective = _brief_objective(brief)
    # The infra-owned acceptance spec (gated worker + accept verifier) and the
    # rework feedback (a re-dispatched worker after reject_work), both threaded on
    # the brief by the orchestrator. NULL ⇒ the legacy un-gated path.
    accept_spec = _brief_accept_spec(brief)
    rework_reason = _brief_rework_reason(brief)

    context_md = ""
    if mode != "verify":
        context_md = render_brief(
            task_id=task_id, mode=mode, brief=brief,
            contracts_in=contracts_in, contracts_out=contracts_out,
            prereadings=prereadings, attached=attached, objective=objective,
            accept_spec=accept_spec, rework_reason=rework_reason,
        )

    # Provision the unit FIRST — the workdir must exist before the spawn, and
    # this is where CONTEXT.md + the pre-readings land. Idempotent on the slug,
    # so a verify/worker stage re-activates the same unit (keeping the plan
    # stage's deliverables).
    workspace_path = await _workspace_create(
        unit_slug, context_md, prereadings, repos)

    # Wait-for-user worker: an ATTENDED worker sits idle until the human drives
    # it — no auto-kickoff. Keyed on born/currently-attached (the objective record
    # replaces goal-emptiness as the wait signal): a drop-in born attended idles
    # for the first steering turn, and a crashed attended placement comes back
    # attended and likewise waits for the human. A detached (re)placement kicks off
    # normally.
    wait_for_user = mode == "worker" and attached

    # Build the kickoff. verify reads the staged plan off disk (it can't itself).
    if mode == "verify":
        plan_text = _read_staged_plan(workspace_path)
        kickoff = _verify_kickoff(
            task_id=task_id, objective=objective, goal=_brief_goal(brief),
            contracts_out=contracts_out, plan_text=plan_text)
    else:
        kickoff = _kickoff_text(task_id=task_id, mode=mode)

    # Spawn live via the EXISTING path so a human can attach via 'transcript'.
    # bypassPermissions removes approval prompts; the per-mode allowlist is what
    # scopes the tools (fs + worker MCP) on claude.
    #
    # UNIFORM placement (T5): every placement — attended or detached — defaults
    # to the SAME driver, ``claude`` / ``haiku`` / ``medium`` effort, resolved by
    # :func:`_resolve_driver` (args > env > saved contract > the field default).
    # There is no attach-state fork; detached nodes are not second-class (same
    # harness, terminal, /compact, toolset). Override the harness via
    # ``args['harness']`` / ``AWM_PLACEMENT_HARNESS`` (opencode stays available),
    # the model via ``args['model']`` / ``AWM_PLACEMENT_MODEL``, and the effort via
    # ``args['effort']`` / ``AWM_PLACEMENT_EFFORT``. NOTE: the per-mode
    # ``allowed_tools`` profile is enforced only on claude (``--allowedTools``);
    # under opencode it's threaded but not yet honored, so the read-only/no-fs
    # guarantee for plan/verify/planner is a claude-only property today (worker is
    # full-tool regardless). Tracked as a follow-up.
    harness, model, effort = _resolve_driver(args)
    # create_session validates effort against the allowed set before the claude
    # backend renders it as ``--effort``.
    # The lifecycle reuses one unit across stages; the prior stage's subprocess
    # may still be retiring on this scope. Wait for it to vacate before spawning.
    await _await_scope_free(unit_slug)
    session = await ai.create_session(
        scope=unit_slug,
        agent_cli=harness, permission_mode="bypassPermissions",
        mode=mode, task_ref=task_id, agent_ref=agent_ref,
        placement_token=placement_token,
        workdir=workspace_path,
        allowed_tools=_allowed_tools_for(mode),
        disallowed_tools=_disallowed_tools_for(mode),
        model=model,
        effort=effort,
    )

    # Record the placement spec on the row so the relays + supervisor can resolve
    # the workdir, contracts, and progress purely from the DAO.
    ai._get_dao().merge_instance_data(session.id, {
        "placement": {
            "mode": mode, "workspace_path": workspace_path,
            "contracts_in": contracts_in, "contracts_out": contracts_out,
            "brief": brief,
        },
        # The agent-domain verbs this mode may invoke — read back by
        # ``ensure_verb_allowed`` to gate the collapsed ``mcp__awm__agent`` tool
        # server-side (the only enforcement under the default opencode harness).
        "allowed_verbs": _allowed_verbs_for(mode),
        "staged": {}, "done": False,
        "graph": {"subtasks": [], "dependencies": [], "contracts": []},
        "attached": attached,
        "paused": paused,
        # Steering-handshake state, cached live off the durable orchestrator row
        # (the same way ``attached`` / ``paused`` are). The consent bits reset on
        # every (re)placement — a fresh agent must re-confirm readiness — while the
        # ratified ``objective`` persists via the brief.
        "steer_user_done": False,
        "steer_agent_ready": False,
        "steer_requested": bool(args.get("steer_requested")),
        "objective": objective,
    })

    # Suppress the kickoff for a wait-for-user worker so it idles until the first
    # human turn arrives through ``enqueue_input``; every other placement is woken
    # immediately. ``paused`` already keeps the supervisor from nagging it.
    if not wait_for_user:
        ai.enqueue_input(session, "orchestrator", kickoff)

    return {"agent_ref": agent_ref, "unit_slug": unit_slug,
            "scope": unit_slug, "placement_token": placement_token}


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

class PlacementError(Exception):
    """Raised when a placement_token is unknown or its placement is closed."""


def _resolve_open_placement(token: str | None) -> dict:
    """Resolve a placement_token to its OPEN placement row, else raise.

    The INTERNAL resolver (the supervisor / liveness paths know a session's own
    token). The model-facing B-op relays resolve from caller identity instead —
    see :func:`_resolve_open_placement_for`. Rejects a missing token, an unknown
    token, and an already-closed placement (a terminal tool already fired or
    supervision force-failed it)."""
    if not token:
        raise PlacementError("missing placement_token")
    dao = ai._get_dao()
    row = dao.resolve_placement(token)
    if row is None:
        raise PlacementError("unknown placement_token")
    if _row_data(row).get("placement_outcome"):
        raise PlacementError("placement already closed")
    return row


def _resolve_open_placement_for(as_: str | None) -> dict:
    """Resolve a B-op call to its OPEN placement from the CALLER IDENTITY.

    ``as_`` IS the placed agent's identity — the unit slug (globally unique),
    stamped onto every MCP call by its own ``awm-mcp`` proxy (the per-placement
    ``AWM_AS`` env → ``X-Awm-As`` header → ``as_``), so the model never types a
    token. The slug IS the placement's scope, so this is a direct lookup of the
    one live placement on that scope. Rejects a missing identity, an identity
    with no open placement, and an already-closed placement."""
    if not as_:
        raise PlacementError("missing caller identity (no AWM_AS on the call)")
    row = ai._get_dao().get_open_placement_by_identity(as_)
    if row is None:
        raise PlacementError(f"no open placement for {as_}")
    if _row_data(row).get("placement_outcome"):
        raise PlacementError("placement already closed")
    return row


def _row_data(row: dict) -> dict:
    try:
        return json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        return {}


def _spec(row: dict) -> dict:
    return _row_data(row).get("placement") or {}


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

async def _retire_worker(instance_id: int) -> None:
    """Stop the agent subprocess (its stage is done). Best-effort."""
    try:
        await ai.stop_session(instance_id)
    except Exception:  # noqa: BLE001
        log.warning("stop_session failed for instance %s", instance_id,
                    exc_info=True)


async def _close_and_retire(row: dict, outcome: str) -> None:
    """Close the placement, retain the unit (keep work), retire the subprocess."""
    ai._get_dao().close_placement(row["id"], outcome=outcome)
    await _retain_unit(row["scope"])
    await _retire_worker(row["id"])


async def stop_placement(args: dict, as_: str | None = None) -> dict:
    """Orchestrator-called stop seam (manifest-omitted, privileged).

    Best-effort stop of a live placement, resolved by its unit slug (``scope``
    / ``workspace_slug``) or its ``task_id`` — the same identities the
    orchestrator↔agents calls use (``place_on_task`` keys by ``unit_slug`` +
    ``task_id``). ``orch_cancel`` calls this after routing the task through the
    give-up path.

    ORDERING IS THE POINT: it closes/finalizes the placement row FIRST (via
    :func:`_close_and_retire`, which closes then retains then kills), so the
    placement is no longer *open* from the orchestrator's perspective the moment
    before the session dies — a dying agent's late ``fail`` / ``deliver`` report
    then resolves to a closed placement and is rejected, and cannot re-route
    consumers a second time. THEN the live tmux/claude session is torn down via
    the existing session-close machinery (``ai.stop_session``).

    Idempotent: no live placement (unknown slug/task, or an already-closed
    placement) is a logged no-op returning ``{ok: True, stopped: False}``. A
    stop failure is the orchestrator's problem to swallow (it wraps the call
    best-effort); the reclaim machinery eventually collects any corpse.
    """
    scope = (args.get("scope") or args.get("workspace_slug")
             or args.get("unit_slug"))
    task_id = args.get("task_id")
    dao = ai._get_dao()
    row = None
    if scope:
        row = dao.get_open_placement_by_identity(scope)
    if row is None and task_id:
        row = dao.get_open_placement_by_task(task_id)
    if row is None:
        return {"ok": True, "stopped": False, "reason": "no live placement"}
    if _row_data(row).get("placement_outcome"):
        # Already logically closed — the row-close already fired, so nothing is
        # routable; leave the corpse to the reclaim machinery.
        return {"ok": True, "stopped": False, "reason": "already closed",
                "task_id": row["task_ref"], "scope": row["scope"]}
    # Close the row BEFORE killing the session (the ordering that stops a late
    # fail from the dying agent re-routing consumers again).
    await _close_and_retire(row, "cancelled")
    log.info("stop_placement: closed + retired placement on %s (task %s)",
             row["scope"], row["task_ref"])
    return {"ok": True, "stopped": True, "task_id": row["task_ref"],
            "scope": row["scope"]}


# ---------------------------------------------------------------------------
# Deliverable staging (contract D) + the cheap structural screen
# ---------------------------------------------------------------------------

def _deliverable_relpath(contract: str) -> str:
    return f"deliverable/{contract}/payload"


def _screen(workspace_path: str, relpath: str) -> bool:
    """Cheap structural screen: the staged deliverable exists and is non-empty.

    The payload schema is deliberately loose for now — this is "non-empty /
    present", not a semantic check."""
    if not workspace_path or not relpath:
        return False
    p = Path(workspace_path) / relpath
    try:
        if p.is_file():
            return p.stat().st_size > 0
        if p.is_dir():
            return any(p.iterdir())
    except OSError:
        return False
    return False


def _register_deliverable(row: dict, contract: str, workspace_path: str,
                          relpath: str) -> str:
    """Resolve a staged deliverable to a ``payload_ref`` for ``orch.deliver``.

    SEAM: today the ref is the absolute on-disk path of the staged payload. When
    the payload/artifact schema firms up this is where artifact registration
    (``artifacts.register`` → an artifact id) will slot in — the orchestrator
    contract (``orch.deliver(contract, payload_ref)``) does not change."""
    return str(Path(workspace_path) / relpath)


async def relay_edit_deliverable(args: dict, as_: str | None = None) -> dict:
    """Worker/plan tool: stage (replace) the content for one output contract.

    Writes to the unit's ``deliverable/<contract>/payload`` and AUTO-CLEARS the
    done flag (editing after indicating done un-finishes the placement)."""
    row = _resolve_open_placement_for(as_)
    contract = args.get("contract") or "default"
    content = args.get("content")
    if content is None:
        raise PlacementError("edit_deliverable requires content")
    spec = _spec(row)
    wp = spec.get("workspace_path") or ""
    relpath = _deliverable_relpath(contract)
    dest = Path(wp) / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(str(content), encoding="utf-8")

    data = _row_data(row)
    staged = dict(data.get("staged") or {})
    staged[contract] = relpath
    ai._get_dao().merge_instance_data(row["id"], {"staged": staged, "done": False})
    return {"ok": True, "staged": sorted(staged), "done": False}


async def relay_indicate_done(args: dict, as_: str | None = None) -> dict:
    """Worker/plan/planner tool: mark the deliverable complete.

    A pure flag — the actual acceptance check runs at the next turn-stop
    boundary (so the harness validates a settled deliverable, not a mid-turn
    one)."""
    row = _resolve_open_placement_for(as_)
    ai._get_dao().merge_instance_data(row["id"], {"done": True})
    return {"ok": True, "done": True}


async def relay_task_fail(args: dict, as_: str | None = None) -> dict:
    """Any mode: give up. Relay to ``orch.fail`` (resolved refs), then retain."""
    row = _resolve_open_placement_for(as_)
    ack = await orch_client.fail(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        reason_type=args.get("reason_type"), reason_text=args.get("reason_text"),
        partial_ref=args.get("partial_ref"),
    )
    await _close_and_retire(row, "failed")
    return {"ok": True, "outcome": "failed", "ack": ack}


# ---------------------------------------------------------------------------
# Steering handshake (durable attach + mutual-detach consent + objective record)
# ---------------------------------------------------------------------------
# The agents service is the SINGLE writer of the handshake transition; the
# orchestrator only mirrors. Every mutation writes the live in-memory flag on the
# placement row (so the supervisor sees it at once) AND mirrors the durable state
# to the orchestrator via ``set_steering`` (so it shows in ``orch_dag`` and
# survives a redispatch). ``on_turn_boundary`` re-checks the handshake too, so a
# bit set from either direction converges deterministically on the single loop.


async def _mirror_steering(**patch) -> None:
    """Best-effort durable mirror of steering fields to the orchestrator.

    ``patch`` names any of ``attached`` / ``steer_user_done`` /
    ``steer_agent_ready`` / ``steer_requested`` / ``objective`` (plus the routing
    ``task_id`` / ``agent_ref``); the orchestrator patches only those. Never
    raises — a transient mirror failure must not wedge the live handshake."""
    try:
        await orch_client.set_steering(**patch)
    except Exception:  # noqa: BLE001
        log.warning("set_steering mirror failed (%s)", patch, exc_info=True)


async def _run_handshake(instance_id: int) -> bool:
    """If both consent bits agree on the live row → auto-detach (idempotent).

    Fires the mutual-detach transition: the objective was already written (by
    ``request_detach``), so this just clears ``attached`` + all three steering
    bits, live and durable, so the autonomous leg proceeds (the supervisor
    unfreezes). Idempotent — a second call sees the bits cleared and no-ops; the
    single event loop serializes simultaneous bit-sets so there is no race."""
    dao = ai._get_dao()
    row = dao.get_instance(instance_id)
    if row is None:
        return False
    data = _row_data(row)
    if data.get("placement_outcome"):
        return False
    if not (data.get("steer_user_done") and data.get("steer_agent_ready")):
        return False
    dao.merge_instance_data(instance_id, {
        "attached": False, "steer_user_done": False,
        "steer_agent_ready": False, "steer_requested": False,
    })
    await _mirror_steering(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        attached=False, steer_user_done=False, steer_agent_ready=False,
        steer_requested=False)
    log.info("steering: mutual-detach handshake fired for %s", row["task_ref"])
    return True


async def relay_request_detach(args: dict, as_: str | None = None) -> dict:
    """Placed-agent tool: consent to detach, refreshing the objective record.

    Writing the objective and setting the agent's detach-consent bit are ONE act:
    the payload IS the refreshed objective record (markdown: intent, constraints,
    what done looks like, non-goals). Rejected unless the task is attached (there
    is no steering session to end otherwise). Runs the handshake immediately, so
    if the user's 'done' bit is already set the node detaches on this call."""
    row = _resolve_open_placement_for(as_)
    if not _row_data(row).get("attached"):
        raise PlacementError(
            "request_detach requires the task to be attached (no human is "
            "steering this node)")
    objective = args.get("objective")
    if objective is None or not str(objective).strip():
        raise PlacementError(
            "request_detach requires the refreshed objective record "
            "(objective=\"<markdown: intent, constraints, done, non-goals>\")")
    dao = ai._get_dao()
    dao.merge_instance_data(row["id"], {
        "objective": str(objective), "steer_agent_ready": True})
    await _mirror_steering(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        objective=str(objective), steer_agent_ready=True)
    detached = await _run_handshake(row["id"])
    return {"ok": True, "steer_agent_ready": True, "detached": detached}


async def relay_rescind_detach(args: dict, as_: str | None = None) -> dict:
    """Placed-agent tool: withdraw the agent's detach-consent bit.

    Rejected unless attached (nothing to rescind while detached). The objective
    record is left as-is — only the readiness bit clears, so the agent keeps
    steering until it re-confirms."""
    row = _resolve_open_placement_for(as_)
    if not _row_data(row).get("attached"):
        raise PlacementError(
            "rescind_detach requires the task to be attached")
    ai._get_dao().merge_instance_data(row["id"], {"steer_agent_ready": False})
    await _mirror_steering(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        steer_agent_ready=False)
    return {"ok": True, "steer_agent_ready": False}


async def relay_request_steering(args: dict, as_: str | None = None) -> dict:
    """Placed-agent tool: flag mid-run that this node wants human steering.

    Sets ``steer_requested`` durably — the wants-steering signal the attention
    strip surfaces. Allowed WHILE DETACHED (that is its whole point): it does not
    freeze the agent, which continues with its best judgment until a human
    actually attaches (an explicit interrupt)."""
    row = _resolve_open_placement_for(as_)
    ai._get_dao().merge_instance_data(row["id"], {"steer_requested": True})
    await _mirror_steering(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        steer_requested=True)
    return {"ok": True, "steer_requested": True}


# ---------------------------------------------------------------------------
# User-side steering (by slug — the UI's explicit-interrupt attach + done bit)
# ---------------------------------------------------------------------------


async def attach(scope: str, task_id: str | None = None) -> dict:
    """Explicit-interrupt attach by unit slug (the UI's attach control / first
    message): set durable ``attached`` and clear ``steer_requested``.

    The live placement's in-memory ``attached`` flag flips so the supervisor
    freezes at the next turn boundary; the durable mirror always runs (keyed on
    ``task_id`` — passed by the UI, or read off the live row) so it survives a
    redispatch even with no live session. Attaching answers a wants-steering
    request, so ``steer_requested`` clears. Best-effort on the mirror."""
    session = ai.get_session_by_scope(scope)
    token = getattr(session, "placement_token", None) if session else None
    dao = ai._get_dao()
    row = dao.resolve_placement(token) if token else None
    if row is not None:
        dao.merge_instance_data(row["id"], {"attached": True,
                                            "steer_requested": False})
        task_id = task_id or row["task_ref"]
    if task_id:
        await _mirror_steering(task_id=task_id, attached=True,
                               steer_requested=False)
    return {"ok": True, "scope": scope, "attached": True}


async def set_user_done(scope: str, done: bool,
                        task_id: str | None = None) -> dict:
    """User-side detach consent by unit slug: set/clear ``steer_user_done``.

    Rejected (``ok: False``) unless the task is attached — there is no steering
    session to finish otherwise. Runs the handshake, so if the agent's readiness
    bit is already set the node detaches on this call. There is deliberately NO
    user-side force-detach: the human can only offer consent, never leave
    unilaterally (the escape hatches are cancel / decompose / re-plan)."""
    session = ai.get_session_by_scope(scope)
    token = getattr(session, "placement_token", None) if session else None
    dao = ai._get_dao()
    row = dao.resolve_placement(token) if token else None
    if row is None:
        return {"ok": False, "error": f"no open placement for {scope}"}
    if not _row_data(row).get("attached"):
        return {"ok": False,
                "error": "set_user_done requires the task to be attached"}
    dao.merge_instance_data(row["id"], {"steer_user_done": bool(done)})
    await _mirror_steering(task_id=task_id or row["task_ref"],
                           steer_user_done=bool(done))
    detached = await _run_handshake(row["id"])
    return {"ok": True, "scope": scope, "steer_user_done": bool(done),
            "detached": detached}


def note_user_post(session) -> None:
    """A new browser user message arrived — clear the agent's detach-readiness.

    The ``enqueue_input`` chokepoint calls this for every browser-originated post
    (one carrying a ``client_id``): new information means the agent must re-confirm
    readiness, so its ``steer_agent_ready`` bit clears. No-op unless the task is
    attached and the bit is set. The live flag is cleared synchronously; the
    durable mirror is scheduled best-effort on the running loop (this runs on the
    loop thread from the ``agent_post`` handler)."""
    token = getattr(session, "placement_token", None)
    if not token:
        return
    dao = ai._get_dao()
    row = dao.resolve_placement(token)
    if row is None:
        return
    data = _row_data(row)
    if not data.get("attached") or not data.get("steer_agent_ready"):
        return
    dao.merge_instance_data(row["id"], {"steer_agent_ready": False})
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Off-loop caller (sync handler in a worker thread): the live flag is
        # cleared but the durable mirror cannot be scheduled — the UI poll goes
        # stale. Callers must reach this on the loop thread (async handler).
        log.warning("note_user_post off-loop for %s: durable steering mirror "
                    "skipped", row["task_ref"])
        return
    loop.create_task(_mirror_steering(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        steer_agent_ready=False))


# ---------------------------------------------------------------------------
# Verifier verdict tools (terminal on call)
# ---------------------------------------------------------------------------

async def relay_approve_plan(args: dict, as_: str | None = None) -> dict:
    """Verifier tool: the plan satisfies the objective (VERIFYING_PLAN → ACTIVE)."""
    row = _resolve_open_placement_for(as_)
    ack = await orch_client.approve_plan(
        task_id=row["task_ref"], agent_ref=row["agent_ref"])
    await _close_and_retire(row, "approved")
    return {"ok": True, "outcome": "approved", "ack": ack}


async def relay_reject_plan(args: dict, as_: str | None = None) -> dict:
    """Verifier tool: the plan does not satisfy the objective (→ re-plan)."""
    row = _resolve_open_placement_for(as_)
    ack = await orch_client.reject_plan(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        reason=args.get("reason"))
    await _close_and_retire(row, "rejected")
    return {"ok": True, "outcome": "rejected", "ack": ack}


async def relay_accept_work(args: dict, as_: str | None = None) -> dict:
    """Accept verifier tool: the claimed delivery passes (VERIFYING_WORK → done).

    Promotes the worker's claim into a real delivery and completes the task. The
    ``evidence`` (what the verifier ran and observed) is passed through; the
    orchestrator falls back to its reason_text when omitted."""
    row = _resolve_open_placement_for(as_)
    ack = await orch_client.accept_work(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        evidence=args.get("evidence"))
    await _close_and_retire(row, "accepted")
    return {"ok": True, "outcome": "accepted", "ack": ack}


async def relay_reject_work(args: dict, as_: str | None = None) -> dict:
    """Accept verifier tool: the claimed delivery fails (VERIFYING_WORK → re-work).

    Loops the task back to the worker under a bounded budget (the worker is
    re-placed with the rejection reason), or fails the task when the budget is
    exhausted — the orchestrator owns that routing."""
    row = _resolve_open_placement_for(as_)
    ack = await orch_client.reject_work(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        reason=args.get("reason"), partial_ref=args.get("partial_ref"))
    await _close_and_retire(row, "rejected")
    return {"ok": True, "outcome": "rejected", "ack": ack}


# ---------------------------------------------------------------------------
# Attach-gated admin tools (DAG restructuring; configurable registry)
# ---------------------------------------------------------------------------


async def relay_admin(op_name: str, args: dict, as_: str | None = None) -> dict:
    """The ONE uniform attach-gate over the admin registry (contract D → B).

    Resolves the caller's placement from its identity, enforces the registry's
    ``requires_attached`` (rejecting when no human is attached — re-checked HERE
    every call, since a human can detach mid-turn), then forwards to the named
    orchestrator op as the resolved ``{task_id, agent_ref}``. Adding/removing/
    renaming an admin command is a single edit in :mod:`admin_ops` — this relay
    never changes."""
    op = admin_ops.BY_NAME.get(op_name)
    if op is None:
        raise PlacementError(f"unknown admin op {op_name!r}")
    row = _resolve_open_placement_for(as_)
    if op.get("requires_attached") and not _row_data(row).get("attached"):
        raise PlacementError(
            f"{op_name} is an attached-only command — no human is attached to "
            "this node")
    payload: dict = {"task_id": row["task_ref"], "agent_ref": row["agent_ref"]}
    for k in op.get("forward", []):
        if args.get(k) is not None:
            payload[k] = args[k]
    ack = await orch_client.call_op(op["orch_op"], **payload)
    return {"ok": True, "op": op_name, "ack": ack}


# ---------------------------------------------------------------------------
# Planner graph buffer tools (committed at accept)
# ---------------------------------------------------------------------------

def _graph(row: dict) -> dict:
    g = _row_data(row).get("graph") or {}
    g.setdefault("subtasks", [])
    g.setdefault("dependencies", [])
    g.setdefault("contracts", [])
    return g


async def relay_add_subtask(args: dict, as_: str | None = None) -> dict:
    """Planner tool: buffer a subtask into the pending sub-DAG."""
    row = _resolve_open_placement_for(as_)
    g = _graph(row)
    sub_id = args.get("id") or f"sub-{len(g['subtasks']) + 1}"
    g["subtasks"].append({
        "id": sub_id, "objective": args.get("objective") or args.get("brief") or "",
        "contracts_out": args.get("contracts_out") or [],
        "title": args.get("title") or "",
    })
    ai._get_dao().merge_instance_data(row["id"], {"graph": g})
    return {"ok": True, "id": sub_id, "subtasks": len(g["subtasks"])}


async def relay_add_dependency(args: dict, as_: str | None = None) -> dict:
    """Planner tool: buffer a dependency edge (``from_id`` → ``to_id``).

    ``from_id`` is the upstream producer, ``to_id`` the downstream consumer (the
    edge points toward the sink). When ``from_id`` produces more than one
    contract, name the one ``to_id`` consumes via the optional ``contract``;
    otherwise it is inferred from ``from_id``'s sole output at commit."""
    row = _resolve_open_placement_for(as_)
    g = _graph(row)
    frm = args.get("from_id") or args.get("from")
    to = args.get("to_id") or args.get("to")
    if not frm or not to:
        raise PlacementError("add_dependency requires from_id and to_id")
    edge = {"from": frm, "to": to}
    contract = args.get("contract")
    if contract:
        edge["contract"] = contract
    g["dependencies"].append(edge)
    ai._get_dao().merge_instance_data(row["id"], {"graph": g})
    return {"ok": True, "dependencies": len(g["dependencies"])}


async def relay_define_contract(args: dict, as_: str | None = None) -> dict:
    """Planner tool: buffer a contract definition for the sub-DAG."""
    row = _resolve_open_placement_for(as_)
    g = _graph(row)
    name = args.get("name")
    if not name:
        raise PlacementError("define_contract requires name")
    g["contracts"].append(dict(args))
    ai._get_dao().merge_instance_data(row["id"], {"graph": g})
    return {"ok": True, "contracts": len(g["contracts"])}


async def relay_search_tasks(args: dict, as_: str | None = None) -> dict:
    """Planner read tool: existing tasks (proxy to the orchestrator)."""
    _resolve_open_placement_for(as_)
    return await orch_client.search_tasks(query=args.get("query"))


async def relay_search_contracts(args: dict, as_: str | None = None) -> dict:
    """Planner read tool: existing contracts (proxy to the orchestrator)."""
    _resolve_open_placement_for(as_)
    return await orch_client.search_contracts(query=args.get("query"))


# ---------------------------------------------------------------------------
# Funnel / acyclicity check for a buffered sub-DAG
# ---------------------------------------------------------------------------

def funnels(subtasks: list, dependencies: list) -> bool:
    """True if the sub-DAG is acyclic and funnels into a single sink.

    Funnel invariant (the user's requirement): every submitted node must reach
    the task being decomposed. We model that as: the child graph is acyclic and
    has exactly one sink (a node with no outgoing edge among the children), and
    every node can reach it. The lone sink is the node that hands back to the
    decomposing task."""
    ids = [s.get("id") for s in subtasks if s.get("id")]
    if not ids:
        return False
    idset = set(ids)
    adj: dict[str, list[str]] = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for e in dependencies:
        frm, to = e.get("from"), e.get("to")
        if frm in idset and to in idset:
            adj[frm].append(to)
            indeg[to] += 1

    # Acyclicity via Kahn's algorithm.
    queue = [i for i in ids if indeg[i] == 0]
    seen = 0
    indeg_w = dict(indeg)
    q = list(queue)
    while q:
        n = q.pop()
        seen += 1
        for m in adj[n]:
            indeg_w[m] -= 1
            if indeg_w[m] == 0:
                q.append(m)
    if seen != len(ids):
        return False  # a cycle

    # Exactly one sink (out-degree 0), and every node reaches it.
    sinks = [i for i in ids if not adj[i]]
    if len(sinks) != 1:
        return False
    sink = sinks[0]
    # Reverse-reachability from the sink must cover every node.
    radj: dict[str, list[str]] = {i: [] for i in ids}
    for frm, tos in adj.items():
        for to in tos:
            radj[to].append(frm)
    stack = [sink]
    reached = set()
    while stack:
        n = stack.pop()
        if n in reached:
            continue
        reached.add(n)
        stack.extend(radj[n])
    return reached == idset


# ---------------------------------------------------------------------------
# Translate the agents-buffered sub-DAG -> the orchestrator decompose_commit shape
# ---------------------------------------------------------------------------

def _translate_subdag(subtasks: list, deps: list,
                      contract_defs: list) -> tuple[dict | None, str | None]:
    """Map the agents-native buffered sub-DAG to the orchestrator's commit shape.

    Agents-native (what the planner tools buffer):
      * ``subtasks``  = ``[{id, objective, contracts_out}]``
      * ``deps``      = ``[{from, to, contract?}]``  (``from`` produces, ``to``
        consumes; the edge points toward the sink)
      * ``contract_defs`` = buffered ``define_contract`` rows (supply specs)

    Orchestrator-native (what ``decompose_commit`` expects):
      * ``children``  = ``[{ref, goal, title?}]``
      * ``contracts`` = ``[{name, spec, producer:<ref>}]``
      * ``edges``     = ``[{consumer:<ref>, contract:<name>}]``

    The orchestrator stays authoritative for acyclicity / funnel / terminal
    derivation; this only restructures. Returns ``(payload, None)`` on success or
    ``(None, reason)`` when a dependency's contract can't be resolved (so the
    caller corrects the planner instead of committing a broken graph)."""
    spec_by_name = {c.get("name"): c.get("spec", "")
                    for c in contract_defs if c.get("name")}
    children = [{"ref": s["id"], "goal": s.get("objective") or "",
                 "title": s.get("title") or ""}
                for s in subtasks]
    produced_by: dict[str, list[str]] = {}
    contracts: list[dict] = []
    for s in subtasks:
        for name in (s.get("contracts_out") or []):
            contracts.append({"name": name, "spec": spec_by_name.get(name, ""),
                              "producer": s["id"]})
            produced_by.setdefault(s["id"], []).append(name)
    edges: list[dict] = []
    for d in deps:
        frm, to = d.get("from"), d.get("to")
        name = d.get("contract")
        if not name:
            names = produced_by.get(frm) or []
            if len(names) == 1:
                name = names[0]
            elif not names:
                return None, (
                    f"dependency {frm}->{to} has no contract: subtask {frm!r} "
                    "produces nothing for the consumer to depend on — give it a "
                    "contracts_out")
            else:
                return None, (
                    f"dependency {frm}->{to} is ambiguous: subtask {frm!r} "
                    f"produces {', '.join(names)}; name the contract on the "
                    "dependency")
        edges.append({"consumer": to, "contract": name})
    return {"children": children, "contracts": contracts, "edges": edges}, None


# ---------------------------------------------------------------------------
# Per-mode acceptance (run at the turn-stop boundary by on_turn_boundary)
# ---------------------------------------------------------------------------

async def _accept_deliverable(row: dict, data: dict, spec: dict) -> bool:
    """worker/plan: if done ∧ every contract staged & screened → deliver each.

    Returns True iff the placement was accepted+closed this boundary (so the
    supervisor does NOT decrement the budget or inject a continuation)."""
    if not data.get("done"):
        return False
    wp = spec.get("workspace_path") or ""
    contracts_out = spec.get("contracts_out") or []
    staged = data.get("staged") or {}
    for c in contracts_out:
        rel = staged.get(c)
        if not rel or not _screen(wp, rel):
            return False  # indicated done but an output is missing/empty
    for c in contracts_out:
        payload_ref = _register_deliverable(row, c, wp, staged[c])
        await orch_client.deliver(
            task_id=row["task_ref"], agent_ref=row["agent_ref"],
            contract=c, payload_ref=payload_ref)
    await _close_and_retire(row, "delivered")
    return True


async def _accept_decompose(row: dict, data: dict, session) -> bool:
    """planner: if done → commit the buffered sub-DAG (funnel-checked).

    Returns True iff the boundary was handled (committed, OR corrected the
    planner and cleared done) — either way the supervisor skips its budget step."""
    if not data.get("done"):
        return False
    g = data.get("graph") or {}
    subtasks = g.get("subtasks") or []
    deps = g.get("dependencies") or []
    contract_defs = g.get("contracts") or []

    reason: str | None = None
    payload: dict | None = None
    if not subtasks or not funnels(subtasks, deps):
        reason = ("it must be non-empty, acyclic, and funnel into a single sink "
                  "(every subtask reaching the task being decomposed)")
    else:
        payload, reason = _translate_subdag(subtasks, deps, contract_defs)

    if reason is not None:
        # Done but the graph is empty / doesn't funnel / can't be translated —
        # correct the planner, don't commit.
        ai._get_dao().merge_instance_data(row["id"], {"done": False})
        ai.enqueue_input(
            session, "supervisor",
            f"[supervisor] Your sub-DAG is not ready to commit: {reason}. "
            "Fix the graph, then call indicate_done again.")
        return True

    ack = await orch_client.decompose_commit(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        children=payload["children"], edges=payload["edges"],
        contracts=payload["contracts"])
    log.info("decompose_commit acked for %s: %s", row["task_ref"], ack)
    await _close_and_retire(row, "decomposed")
    return True


# ---------------------------------------------------------------------------
# Supervision driver (per-mode accept + hard turn budget; freeze while attached)
# ---------------------------------------------------------------------------

def _continuation_prompt(mode: str, task_id: str, remaining: int) -> str:
    """The per-turn nudge. Escalates to a save-progress warning near the cap."""
    if remaining <= TASK_WARN_REMAINING:
        return (
            f"[supervisor] {remaining} turn(s) left before task `{task_id}` is "
            f"force-failed. Checkpoint now: stage what you have via "
            f"`edit_deliverable(...)` and `indicate_done()` if you are done, "
            f"or `task_fail(reason_type=\"transient-error\", "
            f"reason_text=\"...\", partial_ref?)` to preserve partial work. Do "
            f"not start new work."
        )
    if mode == "verify":
        hint = ("Reach a verdict: call `approve_plan()` or "
                "`reject_plan(reason)`.")
    elif mode == "accept":
        hint = ("Run the acceptance checks, then `accept_work()` or "
                "`reject_work(reason)`.")
    elif mode == "planner":
        hint = ("Build the sub-DAG (`add_subtask`/`add_dependency`/"
                "`define_contract`), then `indicate_done()` to commit.")
    else:
        hint = ("Stage your outputs with `edit_deliverable(...)`, then "
                "`indicate_done()`.")
    return (
        f"[supervisor] Continue task `{task_id}` ({remaining} turns left). "
        f"{hint} If stuck, `task_fail(...)` is a valid exit."
    )


async def _fail_transient_and_retire(session, task_id: str,
                                     reason_text: str) -> None:
    """Fail a live placement TYPED transient, then close-row-then-kill.

    The shared path for both the turn-budget force-fail and the stall watchdog:
    report ``orch.fail(transient-error)`` (so the orchestrator's existing
    transient-retry routing re-places the leg within the retry cap), THEN close
    the placement row + retain the unit + retire the subprocess — the SAME
    ordering ``stop_placement`` uses, so a dying agent's late ``fail`` / ``deliver``
    resolves to a closed placement and can't re-route consumers a second time."""
    row = ai._get_dao().resolve_placement(session.placement_token)
    await orch_client.fail(
        task_id=task_id, agent_ref=session.agent_ref,
        reason_type="transient-error",
        reason_text=reason_text,
        partial_ref=None,
    )
    if row is not None:
        await _close_and_retire(row, "failed")
    else:  # fallback: close by id + retain by scope
        ai._get_dao().close_placement(session.id, outcome="failed")
        await _retain_unit(session.scope)
        await _retire_worker(session.id)


async def _force_fail(session, task_id: str) -> None:
    """Turn budget exhausted: fail to the orchestrator, retain unit, retire."""
    await _fail_transient_and_retire(
        session, task_id, f"turn budget ({TASK_TURN_BUDGET}) exhausted")


async def on_turn_boundary(session) -> None:
    """Drive one placed agent at an outer-loop turn boundary.

    Called from ``_reader_loop`` on the ``result`` event (one per turn, because
    claude ``--print`` runs one turn per stdin message then stops). Order:

    1. closed/unknown placement → no-op (drive off the acceptance check, not the
       event).
    2. per-mode acceptance: worker/plan deliver, planner commit. If accepted the
       task already advanced — done.
    3. while ATTACHED → freeze: no budget burn, no nag, no force-fail. A
       human-driven node idles politely; their messages still splice in via
       ``agent_post`` → ``enqueue_input`` and the agent replies on its normal
       turn. A real terminal (the acceptance check above, or ``task_fail``) still
       completes the node, so an attended worker that genuinely delivers advances.
    4. otherwise (autonomous) decrement the hard budget and inject the next
       prompt; at 0, force-fail while retaining work.

    Attach GATES the supervisor (P2): on detach (``set_attached(False)``)
    autonomous supervision resumes from the current ``turn_budget`` — freezing
    skips the decrement rather than resetting it, so nothing is lost across an
    attach/detach cycle."""
    token = getattr(session, "placement_token", None)
    if not token:
        return
    try:
        row = _resolve_open_placement(token)
    except PlacementError:
        return  # closed/unknown — nothing to drive
    data = _row_data(row)
    spec = data.get("placement") or {}
    mode = spec.get("mode") or getattr(session, "mode", "worker")
    task_id = row["task_ref"]

    if mode in ("worker", "plan"):
        if await _accept_deliverable(row, data, spec):
            return
    elif mode == "planner":
        if await _accept_decompose(row, data, session):
            return
    # verify has no auto-accept — its verdict is a terminal tool call.

    # Steering handshake at the turn boundary: if both consent bits agree, the
    # node auto-detaches here too (belt-and-suspenders with the per-bit relays).
    # Re-read the row afterwards so the freeze decision below sees the cleared
    # ``attached`` and supervision resumes on THIS boundary.
    if await _run_handshake(row["id"]):
        data = _row_data(ai._get_dao().get_instance(row["id"]) or row)

    # Freeze while a human is driving (``attached``) OR the task is sticky-paused
    # (``paused``): skip the budget decrement / re-prompt / force-fail entirely.
    # The acceptance check above already ran, so a real deliver still advances the
    # task; we only suppress the autonomous nag. Unlike ``attached`` (which drops
    # the moment the human's WS closes), ``paused`` is sticky — it keeps the
    # supervisor out after the human walks away, until explicitly un-paused.
    if data.get("attached") or data.get("paused"):
        return

    # Auto-compact (T5, claude-only): a placement past the context threshold gets
    # a native ``/compact`` injected here — AFTER the freeze check (never while
    # attached or paused). The compact IS this boundary's action, so skip the
    # budget decrement AND the continuation nudge (the ratio only drops once the
    # compact completes; a cooldown prevents re-fire until then).
    if await _maybe_compact(session):
        return

    session.turn_budget -= 1
    remaining = session.turn_budget
    if remaining <= 0:
        await _force_fail(session, task_id)
        return
    ai.enqueue_input(session, "supervisor",
                     _continuation_prompt(mode, task_id, remaining))


async def _maybe_compact(session) -> bool:
    """Inject a native ``/compact`` when a claude placement is past the context
    threshold and off cooldown. Returns True iff a compact was injected (so the
    caller skips this boundary's budget decrement + continuation nudge).

    claude-only: the tmux claude backend passes ``/compact`` through natively
    (via the same ``send_slash`` seam nudges use); opencode has no equivalent, so
    a non-claude session is skipped. Requires a known ``context_max`` (the init
    event stamps it) — with none we can't compute a ratio, so we skip."""
    if getattr(session, "agent_cli", None) != "claude":
        return False
    context_max = getattr(session, "context_max", None)
    used = getattr(session, "context_used", 0) or 0
    if not context_max or context_max <= 0:
        return False
    if (used / context_max) < _compact_threshold():
        return False
    now = time.monotonic()
    last = getattr(session, "last_compact_at", 0.0) or 0.0
    if last and (now - last) < _COMPACT_COOLDOWN_S:
        return False  # still cooling down from the previous compact
    try:
        await ai.send_slash(session.scope, "/compact")
    except Exception:  # noqa: BLE001 — couldn't inject; let the budget path run
        log.warning("auto-compact: /compact injection failed for %s",
                    session.scope, exc_info=True)
        return False
    session.last_compact_at = now
    log.info("auto-compact: injected /compact for %s (context %s/%s)",
             session.scope, used, context_max)
    return True


# ---------------------------------------------------------------------------
# Stall watchdog (T5) — a placement silent past the window is failed + re-placed
# ---------------------------------------------------------------------------
# A hung placement emits NO harness events (``session.last_activity`` stops
# advancing) yet its subprocess is still alive, so ``reclaim_if_dead`` (which
# fires only on process exit) never triggers. The watchdog is the liveness net
# for that case: a periodic sweep fails any live placement silent past
# ``AWM_PLACEMENT_STALL_S`` and kills it, typed transient so the orchestrator
# re-places the same leg. IMMUNE while attached or paused — a frozen steering
# session is silent by design.


async def _stall_sweep_once() -> int:
    """One stall-watchdog pass over every live placement. Returns the number
    failed this pass (for logging / tests).

    For each in-memory session: resolve its OPEN placement row; skip if it is
    already closed or IMMUNE (attached or paused — the durable freeze predicate);
    otherwise, if it has been silent (no harness event) longer than the stall
    window, fail it typed transient and close-row-then-kill it (reusing
    :func:`_fail_transient_and_retire` — the same ordering ``stop_placement``
    uses so a late report can't double-route)."""
    stall_s = _stall_threshold_s()
    now = time.monotonic()
    dao = ai._get_dao()
    failed = 0
    # Snapshot the registry — failing a session mutates it via the waiter loop.
    for session in list(ai._registry_by_id.values()):
        token = getattr(session, "placement_token", None)
        if not token:
            continue  # not a placement (defensive — every session is one today)
        row = dao.resolve_placement(token)
        if row is None:
            continue
        data = _row_data(row)
        if data.get("placement_outcome"):
            continue  # already closed — a terminal fired
        if data.get("attached") or data.get("paused"):
            continue  # immune: a frozen steering / paused session is silent by design
        last = getattr(session, "last_activity", None)
        if last is None:
            continue
        silent = now - last
        if silent < stall_s:
            continue
        log.warning("stall watchdog: placement %s (task %s) silent for %.0fs "
                    "(> %.0fs) — failing transient + re-placing",
                    session.scope, row["task_ref"], silent, stall_s)
        try:
            await _fail_transient_and_retire(
                session, row["task_ref"],
                f"stalled: no harness activity for {int(silent)}s "
                f"(> {int(stall_s)}s stall window)")
            failed += 1
        except Exception:  # noqa: BLE001 — one bad reap must not kill the sweep
            log.warning("stall watchdog: failed to reap %s", session.scope,
                        exc_info=True)
    return failed


async def stall_watchdog_loop() -> None:
    """Background task: sweep for stalled placements every ~60s. Started from the
    agents service ``_on_start`` (inside the event loop)."""
    log.info("agents: stall watchdog started (window=%ss, sweep=%ss)",
             int(_stall_threshold_s()), int(_STALL_SWEEP_S))
    while True:
        try:
            await asyncio.sleep(_STALL_SWEEP_S)
            await _stall_sweep_once()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("agents: stall watchdog sweep failed")


# ---------------------------------------------------------------------------
# Liveness: a crashed subprocess must not strand its task in ACTIVE
# ---------------------------------------------------------------------------

_INTENTIONAL_EXITS = {"stopped", "killed", "compacted"}


async def reclaim_if_dead(session) -> None:
    """If a placement's subprocess exited without a terminal, fail the task.

    Called from ``_waiter_loop`` when a task-bound subprocess ends. A clean
    terminal (deliver/approve/fail/decompose) already closed the placement, so
    this no-ops. An intentional stop/kill/compact (e.g. respawn) is skipped via
    the row's ``intent``. Only a genuine crash — open placement + a ``live``
    intent — is force-failed (``orch.fail(transient-error)`` + retain), so the
    orchestrator can re-place rather than hang in ACTIVE."""
    token = getattr(session, "placement_token", None)
    if not token:
        return
    dao = ai._get_dao()
    row = dao.get_instance(session.id)
    if row is None:
        return
    data = _row_data(row)
    if data.get("placement_outcome"):
        return  # a terminal already fired
    if (data.get("intent") or "live") in _INTENTIONAL_EXITS:
        return  # respawn / deliberate stop — not a crash
    try:
        await orch_client.fail(
            task_id=row["task_ref"], agent_ref=row["agent_ref"],
            reason_type="transient-error",
            reason_text="placement subprocess exited without an outcome",
            partial_ref=None,
        )
    except Exception:  # noqa: BLE001
        log.warning("orch.fail (liveness) failed for %s", row["task_ref"],
                    exc_info=True)
    dao.close_placement(session.id, outcome="failed")
    await _retain_unit(row["scope"])


# ---------------------------------------------------------------------------
# Attach flag (orthogonal to the state machine)
# ---------------------------------------------------------------------------

async def set_attached(scope: str, attached: bool) -> None:
    """Mark/unmark a live placement as user-attached + mirror to the kernel.

    NO LONGER driven by transcript-WS presence — connect/disconnect is pure
    viewing now. The durable orchestrator ``attached`` flag is the single source
    of truth; this helper writes the live in-memory flag and mirrors it. Attach
    GATES the supervisor: while attached, ``on_turn_boundary`` freezes (no budget
    burn / nag / force-fail) so a human-driven node idles politely; on detach
    autonomous supervision resumes from the current budget. The explicit-interrupt
    attach the UI drives is :func:`attach` (which also clears the wants-steering
    bit); this lower-level helper is kept for the direct set/clear path.
    Best-effort throughout."""
    session = ai.get_session_by_scope(scope)
    if session is None or getattr(session, "mode", "conversational") == "conversational":
        return
    token = getattr(session, "placement_token", None)
    if not token:
        return
    try:
        row = ai._get_dao().resolve_placement(token)
        if row is None:
            return
        ai._get_dao().merge_instance_data(row["id"], {"attached": bool(attached)})
        await orch_client.set_attached(
            task_id=row["task_ref"], agent_ref=row["agent_ref"],
            attached=bool(attached))
    except Exception:  # noqa: BLE001
        log.warning("set_attached failed for %s", scope, exc_info=True)


async def set_paused(scope: str, paused: bool,
                     task_id: str | None = None) -> None:
    """Set a task's sticky ``paused`` flag — live placement + durable mirror.

    The UI's pause toggle (keyed on the unit slug). Unlike :func:`set_attached`
    (driven by transcript-WS presence and cleared on disconnect), ``paused`` is
    sticky — it freezes ``on_turn_boundary`` (``attached OR paused``) and stays
    set after the human walks away, until explicitly toggled off.

    When a live placement exists for the slug, its in-memory ``paused`` flag is
    written so the running supervisor freezes/resumes at once. The durable mirror
    to the kernel (``orch_set_paused``) always runs (keyed on ``task_id`` — passed
    by the UI, or read off the live row), so the flag survives a redispatch even
    when no session is currently live. Best-effort throughout."""
    session = ai.get_session_by_scope(scope)
    token = getattr(session, "placement_token", None) if session else None
    try:
        dao = ai._get_dao()
        row = dao.resolve_placement(token) if token else None
        if row is not None:
            dao.merge_instance_data(row["id"], {"paused": bool(paused)})
            task_id = task_id or row["task_ref"]
        if task_id:
            await orch_client.set_paused(task_id=task_id, paused=bool(paused))
    except Exception:  # noqa: BLE001
        log.warning("set_paused failed for %s", scope, exc_info=True)
