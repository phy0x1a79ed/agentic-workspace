---
name: create-scope
tags: [scope, worktree, status, lifecycle]
requires: [debrief]
description: Create a scope, work in its worktree, and complete it
---

# Create Scope

## Create

```bash
awm scope create <project> <scope>                     # branch from default
awm scope create <project> <scope> --from <branch>     # branch from non-default
```

This creates branch `feat/<scope>`, worktree at `projects/<project>/<scope>/`, and a `.awm/` metadata directory inside the worktree. Status starts as `active`.

## Work

```bash
cd projects/<project>/<scope>/
```

The worktree is the agent's CWD. All git operations target `feat/<scope>` in place — no branch switching. Data is reachable via `.awm/data/` and skill catalogs via `.awm/skills/`.

`.awm/data/` is either a **git-annex clone on branch `scope/<scope>`** (isolated
+ versioned; publish with `scope_data_snapshot` then `scope_data_promote`) or —
for projects whose data hasn't been converted — a plain symlink to
`data/<project>/` shared by every scope. `scope_data_status project=<p>
scope=<s>` reports which. See WORKSPACE.md § *Data concurrency*.

For end-of-session logging, follow `skill_get path="awm/debrief.md"`.

## List

```bash
awm scope list                                          # active only
awm scope list --status all                             # include completed
awm scope list --project <project>                      # filter by project
awm scope list --status completed --project <project>
```

## Complete

```bash
awm scope complete <project> <scope>             # mark completed, keep branch
awm scope complete <project> <scope> --merge     # merge feat/<scope> into default + push
```

Sets status to `completed` in the AWM DB. With `--merge`, merges and pushes the feature branch.

## Status values

| Status      | Meaning                              |
|-------------|--------------------------------------|
| `active`    | Currently being worked on            |
| `completed` | Scope finished, ready for cleanup    |
