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
- Past journals tagged with this skill (a `scope_post kind=journal` with `meta.skill_path=...`) reveal a recurring friction point
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

Past journals that followed the skill carry `meta.skill_path`. Use them to see what agents actually ran into:

```
scope_fetch project=<project> scope=<scope> kind=journal order=desc limit=20
```

Then filter the returned entries client-side for `meta.skill_path == "<file_path>"` and read the bodies of the relevant ones. The **Issues** / **Gotchas** bullets and the outcome paragraph are where drift and friction show up. (To search across scopes, add a `query` and drop `scope`.)

If this is a workspace-level skill with no obvious project/scope, skip this step — the code verification in step 3 is the authoritative check anyway.

### 3. Verify every tool call against the live tool surface

awm is a modular gateway: every operation is **projected from a feature service's `ready.api` manifest**, not hand-registered in one dispatch file. An operation exists if some service under `awm/services/<svc>/` declares the underlying function in its manifest and the gateway projects it. Note the MCP surface is **collapsed by domain** — an agent sees one generic `{verb, args}` tool per domain (`scope`, `agent`, `rlm`, …) and learns a domain's verbs via its reserved `describe` verb — while the CLI/HTTP surfaces and the **default** `GET /tools` stay **expanded** (one `<domain>_<verb>` entry per op). For skill verification, the expanded list is exactly what you want.

Two cheap ways to confirm a tool is live:

- **Ask the running gateway** (authoritative — it's the projected surface):

  ```
  curl -s http://127.0.0.1:7819/tools | jq -r '.tools[].name'   # substitute your sandbox port
  ```

  That default `/tools` is the **expanded** per-verb list (still the source of truth for whether an op exists). The MCP *client* now lists the collapsed per-domain tools (`GET /tools?view=domains`); to enumerate a domain's verbs from a client, call its `describe` verb — e.g. `scope(verb="describe")`.

- **Grep the services** for the underlying op/manifest entry:

  ```
  Grep pattern="\"<fn>\"|name=\"<fn>\"" path=awm/services
  ```

  The projected op name is `<service>_<fn>` by default, overridable by a `"tool"` key on the manifest function (see `awm/gateway/awm/gateway/catalog.py::_tool_name`). So an op surfacing as `scope_post` may be op `post` on the `scopes` service with a `tool="scope_post"` override. On the collapsed MCP surface that's the `scope` domain tool with verb `post` (`scope(verb="post", args={…})`); on CLI/HTTP it stays expanded (`awm scope post` / `POST /invoke {name:"scope_post"}`).

**If a tool the skill calls has no match, it's a ghost.** Decide whether to:

- **Drop the call** (the concept was folded into another tool — e.g. the old `room_*`/`inbox_*`/`session_*` families collapsed into `scope_post`/`scope_fetch`/`scope_subscribe` on the `scopes` service).
- **Replace it** with the tool that now covers the same ground.
- **Add it** as a new manifest function on the owning service (see step 5).

### 4. Verify every field name against the live request schema

For each argument the skill passes to a tool, find the owning service under `awm/services/<svc>/` and confirm the field exists. The function's parameters are declared on the service's manifest / operation definition (`operations/*.py` inside the service dist) and persisted by its DAO (`awm.persistence.dao.BaseDAO` subclass).

Common drift patterns:

- Skill uses a pre-rename field name (e.g. `task` where the model is now `scope`).
- Skill passes a key (`outcome="success"`) that is actually a nested `meta` field on `scope_post`, not a top-level parameter.
- Skill uses a CLI flag name where an MCP-style call would use the parameter name. Both can be valid — check the operation's `Param(...)` for the `cli_name` vs `name` split.

Every field the skill references must map to a `Param` in the owning service's operation registry or a column on its DAO.

### 5. If the skill demands a code change, land it first

Sometimes the skill exposes a legitimate gap: the concept is right, but there's no live field to carry it. When this happens, extend the **owning service** before rewriting the skill. The change set, all inside `awm/services/<svc>/`:

1. **Schema** — add the column to the service's `CREATE TABLE` DDL and bump its `schema_version` (each service inits its own DB via `awm.persistence.databases.init_service_db`).
2. **DAO** — persist the field on INSERT and read it back in the row→model mapper (`awm.persistence.dao.BaseDAO` subclass).
3. **Operation / manifest** — add a `Param(...)` so the field is exposed via both MCP and CLI, and (if needed) surface it in any markdown rendering.
4. **`tests/`** — add focused tests for the new field (empty case + set case, round-trip through the service layer) in that dist's own `tests/` dir.
5. Run the dist's suite before touching the skill: `awm/gateway/scripts/run-tests.sh <svc>`.

Only after the code is green should you rewrite the skill to use the new field.

### 6. Rewrite the skill

Edit the file at the absolute path resolved in step 1 (via `Glob pattern="**/skills/<file_path>"`). Guidelines:

- **Show, don't describe.** A concrete command with realistic argument values beats a prose description every time. Include at least one worked example per step.
- **Distinguish free-form from structured fields.** If a tool has both (e.g. `scope_post` has a free-form `body` string plus a structured `meta` JSON object carrying `title` / `outcome` / `skill_path`), call it out explicitly with a per-field definition. Don't let agents guess which is which.
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
- **Updating a skill without running the owning dist's test suite after a schema change.** A schema bump can pass in isolation but break test fixtures that hardcode column lists (each service's fixtures live in its own `tests/conftest.py`).
- **Adding fields to a skill's examples that aren't real `Param`s in the owning service's `operations/*.py`.** The skill becomes a trap — the example will fail the first time an agent copies it.
