---
name: debrief
tags: [session, completion, reflection, debrief]
requires: []
description: End-of-session debrief — journal the session (scope_post kind=journal), register artifacts, refresh
---

# Session Debrief

Run this protocol at the end of a work session when instructed to debrief.

A scope **is** its channel: the session journal, messages, and system events
are all `scope_posts` addressed by `(project, scope)`. Journaling a session is a
`scope_post` with `kind=journal` — there is no separate `session_log` tool, and
journal entries are not "resolved" (the old read-state/resolve model is gone).

> **Call convention.** The `<domain>_<verb>` names below (`scope_post`, `artifact_sync`, `scope_refresh`, …) are **operation** names. On the collapsed MCP surface, call them as the domain tool's verb — `scope(verb="post", args={kind:"journal", …})`, `artifact(verb="sync")`, etc. (`scope(verb="describe")` lists a domain's verbs). On the CLI/HTTP surfaces they stay expanded (`awm scope post …`, `POST /invoke {name:"scope_post"}`).

## Steps

1. **Update project docs.** If the session changed how the project works — new scripts, new workflows, changed conventions, fixed bugs that affect usage — update `AGENTS.md` (or equivalent project docs) to reflect the current state. Docs should describe the world as it is now, not as it was before the session. Skip if no user-facing behavior changed.

2. **Update scope context.** If the session changed how *this scope's* work is framed — new objectives, refined expectations, scope-local conventions, or post-implementation notes worth carrying forward — update `.awm/context.md` to reflect the current state. Only edit `context.md`; `.awm/history.md` and `.awm/artifacts.md` are auto-generated (rebuilt by `scope_refresh` in step 6) and must not be hand-edited. Skip if the scope's framing is unchanged.

3. **Commit outstanding changes.** If there are uncommitted changes from the session, commit them before journaling the debrief. The debrief should describe work that is already landed, not in-flight. Ask the user before committing if unsure.

4. **Journal the session.** Post a `kind=journal` entry to this scope's channel capturing how things went — outcome, what worked, what didn't, decisions, gotchas, and next steps. If you followed a specific skill, record its path in `meta.skill_path` so the reflection is tied back to that skill for later analysis.

   ```
   scope_post project={project} scope={scope} kind=journal \
     author="agent:{project}/{scope}" \
     body="<the narrative — see below>" \
     meta='{"title": "...", "outcome": "success", "skill_path": "awm/debrief.md"}'
   ```

   Field guide:

   - **`author`** — your scope identity, `agent:{project}/{scope}` (e.g. `agent:awm/full-modular-services`).
   - **`body`** — the full narrative, and the only place the *detail* is preserved. Write the outcome paragraph, then list **Decisions**, **Issues**, and **Next steps** as markdown bullets. `history.md` shows only the title + outcome at a glance (see below), so the body is what a future session reads back via `scope_fetch` — put the substance here, one bullet per discrete item.
   - **`meta`** — a JSON object. Keys that `history.md` renders:
     - `title` — short one-line label (< ~80 chars); falls back to the first 80 chars of `body` if omitted.
     - `outcome` — `success` | `partial` | `failure` | `abandoned`; rendered as `[outcome]` next to the title.
     - `skill_path` — the skill you followed (e.g. `awm/debrief.md`); journals are grouped under it. Omit if none.
     - `deviations` / `suggestions` — optional, for skill-improvement; each renders as a bullet under the entry.

   How it renders: `history.md` groups journals by `skill_path` and shows
   `[id] title [outcome]` per entry (plus any `deviations`/`suggestions`). The
   body is **not** in `history.md` — retrieve it with `scope_fetch` (step 5).

5. **Review prior journal entries.** Read recent journals for this scope to pick up open threads — there is no "resolve" step; open items are carried forward by referencing them in this session's journal and acting on them. Newest-first:

   ```
   scope_fetch project={project} scope={scope} kind=journal order=desc limit=10
   ```

   To search across scopes, add a `query` and drop `scope`. Skip if there is no prior history.

6. **Review and update artifacts.** Before registering, search for existing artifacts in the scope to avoid duplicates and clean up stale entries.

   ```
   artifact_search project={project} scope={scope}
   ```

   - **Delete** any artifacts that were replaced or are no longer relevant:
     ```
     artifact_delete artifact_id=<id>
     ```
   - **Register** new outputs (`artifact_register` upserts on an existing `path`):
     ```
     artifact_register project={project} scope={scope} name="..." artifact_type=... path="..." description="..."
     ```
   - Path relative to workspace root (e.g. `data/{project}/figures/...`).
   - Types: figure, dataset, report, model, script, other.
   - Skip this step if no artifacts were produced or changed.

7. **Sync artifacts** so the registry reflects on-disk reality before closing out:

   ```
   artifact_sync
   ```

   This is lazy — a no-op when nothing has changed since the last sync — so it is safe to call on every debrief. When drift is detected it flips any artifact whose file has been deleted to `status='stale'` (hiding it from search), restores any that have reappeared, and prunes their embeddings. Pass `force=true` only if you deleted files out-of-band without any other DB write during this session.

8. **Refresh** so the next session sees your contributions:

   ```
   scope_refresh project={project} scope={scope}
   ```
