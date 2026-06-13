---
name: debrief
tags: [session, completion, reflection, debrief]
requires: []
description: End-of-session debrief — log the session, register artifacts, refresh
---

# Session Debrief

Run this protocol at the end of a work session when instructed to debrief.

## Steps

1. **Update project docs.** If the session changed how the project works — new scripts, new workflows, changed conventions, fixed bugs that affect usage — update `AGENTS.md` (or equivalent project docs) to reflect the current state. Docs should describe the world as it is now, not as it was before the session. Skip if no user-facing behavior changed.

2. **Update scope context.** If the session changed how *this scope's* work is framed — new objectives, refined expectations, scope-local conventions, or post-implementation notes worth carrying forward — update `.awm/context.md` to reflect the current state. Only edit `context.md`; `.awm/history.md` and `.awm/artifacts.md` are auto-generated (rebuilt by `scope_refresh` in step 8) and must not be hand-edited. Skip if the scope's framing is unchanged.

3. **Commit outstanding changes.** If there are uncommitted changes from the session, commit them before logging the debrief. The debrief should describe work that is already landed, not in-flight. Ask the user before committing if unsure.

4. **Log the session.** Capture how things went — what worked, what didn't, what to improve. If you followed a specific skill, pass its path so the reflection is tied back to that skill for later analysis.

   The call has two kinds of fields:

   - **`title`** — a short one-line label (e.g. "Fixed parser crash on empty input"). Shown in `history.md` and search results. Keep it under ~80 characters.
   - **`summary`** — one free-form paragraph. This is the narrative reflection: outcome (success / partial / failure / abandoned), what went well, what didn't, and any deviations or suggestions for a skill you followed.
   - **`skill_path`** — the path of the skill you followed this session, if any (e.g. `awm/debrief.md`). Omit if no skill was followed.
   - **`--decision` / `--issue` / `--next-step`** — repeatable flags. Each occurrence appends **one bullet** to a structured list that a future session will read back. Use one flag per discrete item; do **not** cram multiple items into one string.
     - `--decision` → a concrete choice made this session (what you picked and why)
     - `--issue` → a gotcha, bug, or blocker encountered
     - `--next-step` → a specific TODO the next session should pick up

   Example:

   ```
   session_log project=my-proj scope=add-normalization \
     skill_path="awm/debrief.md" \
     title="Quantile normalization for batches 1-2, batch 3 blocked on missing values" \
     summary="Partial success. Quantile normalization worked on batches 1-2, but batch 3 has too many missing values to normalize directly. Debrief skill was clear, no deviations." \
     --decision "Use quantile normalization for cross-sample comparability" \
     --decision "Defer batch 3 handling until imputation strategy is chosen" \
     --issue "Batch 3 has 40% missing values in the expression matrix" \
     --next-step "Evaluate KNN vs MICE imputation on batch 3" \
     --next-step "Re-run normalization once batch 3 is imputed"
   ```

   In the rendered session log, `summary` becomes the `## Session Summary` paragraph and the repeated flags become bullet lists under `## Decisions Made`, `## Gotchas / Issues`, and `## Next Steps`.

5. **Resolve fixed issues.** Check for open session issues from prior sessions that were addressed this session. Search for open entries, then resolve any that are no longer relevant:

   ```
   session_search project={project} scope={scope} status=open
   ```

   For each issue that was fixed or is no longer relevant:

   ```
   session_resolve session_id={id} resolution="Fixed: brief description of what was done"
   ```

   Resolved entries will appear as compact one-liners in `history.md` (with their ID for later retrieval via `session_get`), while open entries remain fully visible. Skip this step if there are no prior open issues.

6. **Review and update artifacts.** Before registering, search for existing artifacts in the scope to avoid duplicates and clean up stale entries.

   ```
   artifact_search project={project} scope={scope}
   ```

   - **Delete** any artifacts that were replaced or are no longer relevant:
     ```
     artifact_delete artifact_id=<id>
     ```
   - **Register** new outputs (skips if `artifact_register` is called with an existing `path` — it upserts):
     ```
     artifact_register project={project} scope={scope} name="..." artifact_type=... path="..." description="..."
     ```
   - Path relative to workspace root (e.g. `data/{project}/figures/...`).
   - Types: figure, dataset, report, model, script, other.
   - Skip this step if no artifacts were produced or changed.

7. **Sync artifacts** so the registry reflects on-disk reality before closing out:

   ```
   artifacts_sync
   ```

   This is lazy — a no-op when nothing has changed since the last sync — so it is safe to call on every debrief. When drift is detected it flips any artifact whose file has been deleted to `status='stale'` (hiding it from search), restores any that have reappeared, and prunes their embeddings. Pass `force=true` only if you deleted files out-of-band without any other DB write during this session.

8. **Refresh** so the next session sees your contributions:

   ```
   scope_refresh project={project} scope={scope}
   ```
