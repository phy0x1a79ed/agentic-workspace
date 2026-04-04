---
name: skill-update
type: protocol
scope: workspace
tags: [skills, improvement, meta, maintenance]
requires: []
description: Revise a skill file based on accumulated experiences
---

# Skill Update Protocol

Follow this protocol to revise a skill based on accumulated experiences.

## Steps

1. **Read the target skill:**
   ```
   skills_get path="..."
   ```

2. **List experiences for that skill:**
   ```
   experience_list skill_path="..."
   ```

3. **Identify patterns** in deviations and suggestions across experiences.

4. **Edit the skill file** — update steps, add gotchas, improve clarity based on what agents actually encountered.

5. **Bump version** in the skill's frontmatter (increment the integer, or add `version: 1` if missing).

6. **Add a changelog entry** at the bottom of the skill:
   ```
   ## Changelog
   - v{N} ({date}): {what changed and why}
   ```

7. **Commit the change:**
   ```
   git add {skill_file}
   git commit -m "skill: update {name} to v{N} based on {N} experiences"
   ```

8. **Reindex:**
   ```
   skills_reindex
   ```
