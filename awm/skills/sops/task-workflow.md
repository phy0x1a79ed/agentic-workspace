---
name: task-workflow
type: sop
tags: [task, workflow, worktree, status, session]
description: Task lifecycle — create, pause, complete — plus session logging
---

# Task Workflow

## Creating a Task

```bash
awm task create <project> <task>
awm task create <project> <task> --from dev   # branch from non-default
```

This creates:
- A feature branch (`feat/<task>`)
- A git worktree at `repos/<project>/<task>/` (clean code only)
- An agent workspace at `main/<project>/tasks/<task>/` with AGENTS.md, symlinks, and results directory
- A task record in the DB with status `active`

### Example

```bash
awm task create my-project add-normalization
# Worktree: repos/my-project/add-normalization/
# Workspace: main/my-project/tasks/add-normalization/
# Branch:    feat/add-normalization
# Status:    active (tracked in DB)
```

## Working in a Task

The agent workspace at `main/<project>/tasks/<task>/` is the working directory for agents. It contains:

| Path | Target |
|------|--------|
| `repo/` | `../../../../repos/<project>/<task>/` (git worktree) |
| `skills/` | `awm/skills/` (package data) |
| `results/` | Local directory for task outputs |
| `../../data` | Project data via the project-level `data` symlink |

Code changes go in the `repo/` symlink (which is the git worktree). Results and outputs go in `results/`.

## Status Management

Task status is tracked in the DB. Use AWM commands to manage it:

| Status      | Meaning                              |
|-------------|--------------------------------------|
| `active`    | Currently being worked on            |
| `paused`    | Work suspended, will resume later    |
| `completed` | Task finished, ready for cleanup     |

```bash
awm task pause <project> <task>
awm task resume <project> <task>
```

## Session Logging

At the end of each work session, log what happened:

```bash
awm session log <project> <task> \
  --summary "Implemented normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1
```

This records the session in the DB (summary, decisions, issues, next steps, agent ID, timestamp).

You can query past sessions:

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
1. Updates task status to `completed` in the DB
2. Optionally merges the feature branch into the default branch and pushes

## Task Lifecycle Summary

```
awm task create  -->  active  -->  [pause <-> resume]  -->  awm task complete
                        |                                         |
                  work in workspace                        merge + cleanup
                  awm session log
```
