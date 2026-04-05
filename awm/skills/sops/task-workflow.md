---
name: scope-workflow
type: protocol
scope: workspace
tags: [scope, workflow, worktree, status, experience, session, lifecycle]
requires: [git-workflow, debrief]
description: Scope lifecycle — create, work, debrief, complete — plus session logging
---

# Task Workflow

## Creating a Task

```bash
awm task create <project> <task>
awm task create <project> <task> --from develop   # branch from non-default
```

This creates a feature branch (`feat/<task>`), a new worktree at `tasks/<project>/<task>/`, symlinks to shared dirs (`data/`, `results/`, `reports/`), and a `.status` file set to `active`.

### Example

```bash
awm task create my-project add-normalization
# Creates: tasks/my-project/add-normalization/
# Branch:  feat/add-normalization
# Status:  active
```

## Working in a Task Worktree

Navigate to the worktree directory and work normally:

```bash
cd tasks/my-project/add-normalization/
# edit files, run code, commit changes
```

All git operations apply to the task branch within that worktree. Shared directories are available via symlinks:

| Symlink | Target |
|---------|--------|
| `data/` | `data/` (workspace-wide shared data) |
| `results/` | `results/<project>/<task>/` |
| `reports/` | `reports/<project>/` |

## Status Conventions

The `.status` file in the worktree root tracks task state:

| Status      | Meaning                              |
|-------------|--------------------------------------|
| `active`    | Currently being worked on            |
| `paused`    | Work suspended, will resume later    |
| `completed` | Task finished, ready for cleanup     |

Use AWM commands to manage status:

```bash
awm task pause <project> <task>
awm task resume <project> <task>
```

## Experience / Session Logging

At the end of each work session, log what happened using the AWM session logger:

```bash
awm session log <project> <task> \
  --summary "Implemented normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1
```

This:
1. Appends a formatted entry to `experiences.md` in the task worktree
2. Commits the change to the feature branch
3. Records the metadata (summary, commit hash, timestamp) in the AWM database

You can also query past sessions for reflection:

```bash
awm session list --project my-project
awm session get <id>
awm session reflect --query "normalization"
```

## Listing Tasks

```bash
awm task list                                  # all active tasks
awm task list --status all                     # all tasks regardless of status
awm task list --project my-project             # tasks for a specific project
awm task list --status completed --project my-project
```

## Completing a Task

```bash
awm task complete <project> <task>             # mark completed, keep branch
awm task complete <project> <task> --merge     # merge feature branch into main
```

This:
1. Sets `.status` to `completed`
2. Updates task status in the AWM database
3. Appends a completion entry to `experiences.md`
4. Optionally merges the feature branch into the default branch and pushes

## Task Lifecycle Summary

```
awm task create  -->  active  -->  [pause <-> resume]  -->  awm task complete
                        |                                         |
                  work in worktree                         merge + cleanup
                  awm session log
```
