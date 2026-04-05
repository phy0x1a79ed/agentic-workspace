---
name: scope-workflow
type: protocol
scope: workspace
tags: [scope, workflow, worktree, status, experience, session, lifecycle]
requires: [git-workflow, debrief]
description: Scope lifecycle — create, work, debrief, complete — plus session logging
---

# Scope Workflow

## Creating a Scope

```bash
awm scope create <project> <scope>
awm scope create <project> <scope> --from develop   # branch from non-default
```

This creates a feature branch (`feat/<scope>`), a new worktree at `projects/<project>/<scope>/`, and a `.awm/` metadata directory.

### Example

```bash
awm scope create my-project add-normalization
# Creates: projects/my-project/add-normalization/
# Branch:  feat/add-normalization
# Status:  active
```

## Working in a Scope Worktree

Navigate to the worktree directory and work normally:

```bash
cd projects/my-project/add-normalization/
# edit files, run code, commit changes
```

All git operations apply to the scope branch within that worktree.

## Status Conventions

The scope status is tracked in the AWM database:

| Status      | Meaning                              |
|-------------|--------------------------------------|
| `active`    | Currently being worked on            |
| `completed` | Scope finished, ready for cleanup    |

## Experience / Session Logging

At the end of each work session, log what happened using the AWM session logger:

```bash
awm session log <project> <scope> \
  --summary "Implemented normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1
```

This:
1. Records a formatted entry in the AWM database
2. Stores metadata (summary, timestamp, agent) for querying

You can also query past sessions for reflection:

```bash
awm session list --project my-project
awm session get <id>
```

## Listing Scopes

```bash
awm scope list                                  # all active scopes
awm scope list --status all                     # all scopes regardless of status
awm scope list --project my-project             # scopes for a specific project
awm scope list --status completed --project my-project
```

## Completing a Scope

```bash
awm scope complete <project> <scope>             # mark completed, keep branch
awm scope complete <project> <scope> --merge     # merge feature branch into main
```

This:
1. Updates scope status to `completed` in the AWM database
2. Optionally merges the feature branch into the default branch and pushes

## Scope Lifecycle Summary

```
awm scope create  -->  active  -->  awm scope complete
                        |                    |
                  work in worktree     merge + cleanup
                  awm session log
```
