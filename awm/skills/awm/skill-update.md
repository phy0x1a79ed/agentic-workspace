---
name: skill-update
tags: [skills, improvement, meta, maintenance]
requires: []
description: Revise a skill to match the live tool surface, fix drift, and land code changes the skill demands
---

# Skill Update Protocol

Skills drift. The tools they call get renamed, merged, or deleted; field names on request schemas change; examples reference flags that no longer exist. A skill update is primarily a **reconciliation with reality**, not a literary revision.

Follow this protocol when:

- An agent hit an error following a skill (tool not found, invalid field, wrong argument shape)
- You notice a skill references a tool or field you can't find in the code
- Past sessions tagged with this skill (via `session_log skill_path=...`) reveal a recurring friction point
- A new capability needs to be exposed through an existing skill

## Steps

### 1. Read the skill

```
skills_get path="awm/<name>.md"
```

Note every tool call, every field name, and every example in the skill body. These are the claims the skill is making about the live system.

### 2. Pull prior reflections on this skill

Past debriefs that followed the skill will have `skill_path` set. Use them to see what agents actually ran into:

```
session_list project=<project> task=<task>
```

Then filter the returned entries client-side for `skill_path == "awm/<name>.md"` and read the ones that look relevant with `session_get session_id=<id>`. The `## Gotchas / Issues` and free-form `summary` sections are where drift and friction show up.

If this is a workspace-level skill with no obvious project/task, skip this step — the code verification in step 3 is the authoritative check anyway.

### 3. Verify every tool call against the live tool surface

For each tool the skill mentions, confirm it is actually registered. MCP tools live in two places in the code:

- **Registry-driven tools** — `awm/operations/*.py` (look for `Operation(name="...")` entries).
- **Hand-registered tools** — `awm/mcp_server.py` (look for `Tool(name="...")` entries).

Grep both:

```
Grep pattern="name=\"<tool>\"" path=awm/operations awm/mcp_server.py
```

**If a tool the skill calls has no match, it's a ghost.** Decide whether to:

- **Drop the call** (the concept was folded into another tool — e.g. `experience_log` was folded into `session_log`).
- **Replace it** with the tool that now covers the same ground.
- **Add it** as a new MCP tool if the capability is genuinely missing (see step 5).

### 4. Verify every field name against the live request schema

For each argument the skill passes to a tool, open the corresponding request model (usually in `awm/models.py`, e.g. `SessionLogCreateRequest`) and confirm the field exists with the expected type.

Common drift patterns:

- Skill uses `{scope}` but the model field is `task` (pre-rename leftover).
- Skill passes `outcome="success"` but no such field or enum exists in the model.
- Skill uses a CLI flag name (`--decision`) where an MCP-style call would use the parameter name (`decisions`). Both can be valid — the CLI flag is defined by `Param(cli_name=...)` in `awm/operations/*.py`, and the MCP name is `Param(name=...)`.

Every field the skill references must map to either a `Param` in the operation registry or a property on the request model.

### 5. If the skill demands a code change, land it first

Sometimes the skill exposes a legitimate gap: the concept is right, but there's no live field to carry it. When this happens, extend the code before rewriting the skill. Use the `debrief.md` → `session_log.skill_path` update (schema v9 → v10) as the reference case. The full change set is:

1. **`awm/models.py`** — add the field to the request model (optional, `None` default) and the entry/response model.
2. **`awm/db.py`** — add the column to the `CREATE TABLE` DDL, add a `(N, N+1)` entry to `MIGRATIONS`, and bump `SCHEMA_VERSION`.
3. **`awm/services/*.py`** — persist the field on INSERT, read it back in the row→model mapper, surface it in any markdown rendering (e.g. `_format_entry`).
4. **`awm/operations/*.py`** — add a `Param(...)` so the field is exposed via both MCP and CLI; optionally add it to list/detail output columns.
5. **`awm/tests/test_<service>.py`** — add focused tests for the new field (empty case + set case, and round-trip through the service layer).
6. Run the full suite before touching the skill: `python3 -m pytest awm/tests/`.

Only after the code is green should you rewrite the skill to use the new field.

### 6. Rewrite the skill

Edit `/home/tony/agentic_workspace/awm/skills/awm/<name>.md` directly. Guidelines:

- **Show, don't describe.** A concrete command with realistic argument values beats a prose description every time. Include at least one worked example per step.
- **Distinguish free-form from structured fields.** If a tool has both (e.g. `session_log` has a free-form `summary` string plus repeatable `--decision` / `--issue` / `--next-step` flags that append to lists), call it out explicitly with a per-field definition. Don't let agents guess which is which.
- **Drop ghost concepts.** If a convention isn't backed by code (version numbers, changelog sections, outcome enums), cut it — or back it with real code in step 5.
- **Keep `{placeholder}` names aligned with the live schema.** If the tool's field is `task`, write `{task}`, not `{scope}`.
- **Trim aggressively.** Every line in a skill is an instruction an agent will try to execute. Redundant steps create drift.

### 7. Update cross-references

```
Grep pattern="<skill-name>|<old tool name>" path=/home/tony/agentic_workspace/awm/skills
```

Check and update as needed:

- `awm/skills/_index.md` — description column. Keep it matched to the skill's frontmatter `description`.
- Other skills whose `requires:` frontmatter or prose references this skill.
- Any skill that re-embeds step numbers or field names from this skill's examples.

### 8. Reindex and verify

```
skills_reindex
skills_get path="awm/<name>.md"
```

Confirm the rewrite is picked up and the index description matches.

### 9. Smoke-test the rewritten skill

Follow the rewritten skill end-to-end against the live MCP server with a throwaway project/task. Every command in every example should execute without error. If any call fails, go back to step 3 — you missed a ghost.

## Anti-patterns

- **Bumping a "version" field in frontmatter.** Not a convention in this workspace. Git history is the changelog.
- **Adding `## Changelog` sections to skills.** Same reason. Use `git log awm/skills/awm/<name>.md`.
- **Editing the skill before verifying the tool surface.** You'll codify the next round of drift.
- **Updating a skill without running the test suite after a schema change.** Migrations can pass in isolation but break test fixtures that hardcode column lists (see `awm/tests/conftest.py`).
- **Adding fields to a skill's examples that aren't real `Param`s in `awm/operations/*.py`.** The skill becomes a trap — the example will fail the first time an agent copies it.
