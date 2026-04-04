---
name: debrief
type: protocol
scope: workspace
tags: [session, completion, reflection, experience, debrief]
requires: []
description: End-of-session debrief — log experiences, register artifacts, reflect
---

# Session Debrief

Run this protocol at the end of a work session when instructed to debrief.

## Steps

1. **Log experience** for each skill you followed this session:
   ```
   experience_log project={project} scope={scope} skill_path="..." summary="..." deviations="..." suggestions="..."
   ```
   - If no specific skill was followed, omit skill_path
   - Be honest about outcome: success, partial_success, failure, abandoned
   - Include deviations (what differed from the protocol) and suggestions (how to improve the skill)

2. **Register artifacts** for any new outputs you created:
   ```
   artifact_register project={project} scope={scope} name="..." artifact_type=... path="..." description="..."
   ```
   - Path should be relative to workspace root (e.g. `data/{project}/figures/...`)
   - Types: figure, dataset, report, model, script, other

3. **Send reflection** to the project inbox:
   ```
   inbox_send scope=project:{project} sender=scope:{project}/{scope} msg_type=reflection subject="..." body="..."
   ```
   - Summarize what was accomplished, what's pending, and any blockers

4. **Log session** for continuity:
   ```
   session_log {project} {scope} --summary "..." --decision "..." --issue "..." --next-step "..."
   ```

5. **Refresh** so the next session sees your contributions:
   ```
   awm_refresh project={project} scope={scope}
   ```
