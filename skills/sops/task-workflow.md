---
name: task-workflow
type: sop
tags: [task, workflow, worktree, status, experience]
---

# Task Workflow

## Creating a Task

```bash
scripts/new-task.sh <project-name> <task-name>
```

This creates a feature branch (`feat/<task-name>`), a new worktree at `projects/<project-name>/<task-name>/`, and a `.status` file set to `active`.

### Example

```bash
scripts/new-task.sh my-project add-normalization
# Creates: projects/my-project/add-normalization/
# Branch:  feat/add-normalization
# Status:  active
```

## Working in a Task Worktree

Navigate to the worktree directory and work normally:

```bash
cd projects/my-project/add-normalization/
# edit files, run code, commit changes
```

All git operations apply to the task branch within that worktree.

## Status Conventions

The `.status` file in the worktree root tracks task state:

| Status      | Meaning                              |
|-------------|--------------------------------------|
| `active`    | Currently being worked on            |
| `paused`    | Work suspended, will resume later    |
| `completed` | Task finished, ready for cleanup     |

Update status manually:

```bash
echo "paused" > .status
echo "active" > .status
```

## Experience Logging

At the end of each work session, append findings to `experiences.md` in the worktree root:

```bash
cat >> experiences.md << 'EOF'
## 2026-03-16 — Session summary title

- What was attempted and outcome
- Obstacles encountered and how they were resolved
- Key decisions and rationale
EOF
```

This captures context for future sessions and other agents picking up the task.

## Completing a Task

```bash
scripts/complete-task.sh <project-name> <task-name>
```

This:
1. Sets `.status` to `completed`
2. Merges the task branch into `main` (or creates a PR)
3. Removes the worktree
4. Deletes the local branch

### Example

```bash
scripts/complete-task.sh my-project add-normalization
```

## Task Lifecycle Summary

```
new-task.sh  -->  active  -->  [paused <-> active]  -->  complete-task.sh
                    |                                         |
              work in worktree                         merge + cleanup
              log experiences
```
