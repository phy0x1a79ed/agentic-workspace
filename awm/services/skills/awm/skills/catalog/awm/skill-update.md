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

### 1. Read the skill and resolve its path dynamically

Never hardcode an absolute path to a skill file. Instead, look it up through the skills service so the protocol is layout-independent:

```
skill_search query="<name>"           # returns hits with file_path relative to the skills root
skill_get    path="<file_path>"       # returns the full skill body + metadata
```

To get the **absolute** path for a subsequent `Edit`, resolve it with Glob rather than guessing:

```
Glob pattern="**/skills/<file_path>"
```

(e.g. `**/skills/awm/debrief.md`). The first match is the on-disk file to edit.

Note every tool call, every field name, and every example in the skill body. These are the claims the skill is making about the live system.

### 2. Pull prior reflections on this skill

Past debriefs that followed the skill will have `skill_path` set. Use them to see what agents actually ran into:

```
session_search project=<project> scope=<scope> status=all
```

Then filter the returned entries client-side for `skill_path == "<file_path>"` and read the ones that look relevant with `session_get session_id=<id>`. The `## Gotchas / Issues` and free-form `summary` sections are where drift and friction show up.

If this is a workspace-level skill with no obvious project/scope, skip this step — the code verification in step 3 is the authoritative check anyway.

### 3. Verify every tool call against the live tool surface

For each tool the skill mentions, confirm it is actually registered. MCP tools live in two places in the code:

- **Registry-driven tools** — `awm/operations/*.py` (look for `Operation(name="...")` entries). These are exposed as MCP tools via `operations_to_mcp_tools(...)` in `awm/tool_dispatch.py`.
- **Hand-registered tools** — `awm/tool_dispatch.py` itself (look for `Tool(name="...")` entries inside `TOOL_DEFINITIONS`, plus the matching `if name == "..."` branch in `handle_tool`).

Grep both:

```
Grep pattern="name=\"<tool>\"" path=awm/operations awm/tool_dispatch.py
```

**If a tool the skill calls has no match, it's a ghost.** Decide whether to:

- **Drop the call** (the concept was folded into another tool — e.g. `experience_log` was folded into `session_log`).
- **Replace it** with the tool that now covers the same ground.
- **Add it** as a new MCP tool if the capability is genuinely missing (see step 5).

### 4. Verify every field name against the live request schema

For each argument the skill passes to a tool, open the corresponding request model (usually in `awm/models.py`, e.g. `SessionLogCreateRequest`) and confirm the field exists with the expected type.

Common drift patterns:

- Skill uses a pre-rename field name (e.g. `task` where the model is now `scope`).
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

Edit the file at the absolute path resolved in step 1 (via `Glob pattern="**/skills/<file_path>"`). Guidelines:

- **Show, don't describe.** A concrete command with realistic argument values beats a prose description every time. Include at least one worked example per step.
- **Distinguish free-form from structured fields.** If a tool has both (e.g. `session_log` has a free-form `summary` string plus repeatable `--decision` / `--issue` / `--next-step` flags that append to lists), call it out explicitly with a per-field definition. Don't let agents guess which is which.
- **Drop ghost concepts.** If a convention isn't backed by code (version numbers, changelog sections, outcome enums), cut it — or back it with real code in step 5.
- **Keep `{placeholder}` names aligned with the live schema.** If the tool's field is `scope`, write `{scope}`, not `{task}`.
- **Trim aggressively.** Every line in a skill is an instruction an agent will try to execute. Redundant steps create drift.

### 7. Update cross-references

```
Grep pattern="<skill-name>|<old tool name>" path=<skills root from step 1>
```

Check and update as needed:

- Other skills whose `requires:` frontmatter or prose references this skill.
- Any skill that re-embeds step numbers or field names from this skill's examples.

The frontmatter `description` is the single source of truth — discovery (`skill_search`, `skill_get`, `find_by_name`) always reads it via a live filesystem scan. There is no separate catalog file to keep in sync.

### 8. Sync the skills index and verify

```
skill_sync
skill_get path="<file_path>"
```

`skill_sync` is lazy — it fingerprints `(file_path, mtime_ns)` across the skills directory and no-ops when nothing has changed, so it is safe to call on every skill-update run. When it detects drift (your edit just bumped the mtime) it re-embeds the changed skill and prunes any embeddings whose source file is now missing. Call it with `force=true` only if you suspect the fingerprint cache is lying.

`skill_get` then confirms the updated body and frontmatter come back.

### 9. Smoke-test the rewritten skill

Follow the rewritten skill end-to-end against the live MCP server with a throwaway project/scope. Every command in every example should execute without error. If any call fails, go back to step 3 — you missed a ghost.

## Anti-patterns

- **Hardcoding an absolute path like `/home/<user>/.../skills/awm/<name>.md`.** Resolve via `skill_search` + `Glob` (step 1) so the protocol works in any worktree.
- **Bumping a "version" field in frontmatter.** Not a convention in this workspace. Git history is the changelog.
- **Adding `## Changelog` sections to skills.** Same reason. Use `git log` on the skill file.
- **Editing the skill before verifying the tool surface.** You'll codify the next round of drift.
- **Updating a skill without running the test suite after a schema change.** Migrations can pass in isolation but break test fixtures that hardcode column lists (see `awm/tests/conftest.py`).
- **Adding fields to a skill's examples that aren't real `Param`s in `awm/operations/*.py`.** The skill becomes a trap — the example will fail the first time an agent copies it.
