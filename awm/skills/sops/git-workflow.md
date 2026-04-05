---
name: git-workflow
type: reference
scope: workspace
tags: [git, bare-repo, worktree, branch, pr, merge]
requires: []
description: Bare repo + worktree model, branching, PRs
---

# Git Workflow

## Bare Repo + Worktree Model

Each project uses a **bare repository** with linked worktrees instead of a standard clone.

```
projects/<project>/
  .bare/             # bare repo (no working directory)
  main/              # worktree -> main branch
  <scope-name>/      # worktree -> feat/<scope-name> branch
```

### Why Bare Repos

- **No checkout conflicts**: A bare repo has no working directory, so switching branches in one worktree never affects another.
- **Parallel work**: Multiple worktrees can be checked out to different branches simultaneously.
- **Clean separation**: Each scope gets its own directory with its own branch; no stashing or context switching required.

## Branch Naming

| Prefix          | Use case                        |
|-----------------|---------------------------------|
| `feat/<scope>`  | New features or enhancements    |
| `fix/<scope>`   | Bug fixes                       |

Branches are created automatically by `awm scope create`.

## Listing Worktrees

```bash
git -C projects/<project>/.bare worktree list
```

## Creating Pull Requests

From within a scope worktree:

```bash
cd projects/<project>/<scope-name>/
git push -u origin feat/<scope-name>
gh pr create --title "feat: <description>" --body "Summary of changes"
```

## Syncing with Upstream (Forked Repos)

Add the upstream remote (done automatically on fork setup):

```bash
git -C projects/<project>/.bare remote add upstream <upstream-url>
```

Fetch and merge upstream changes into main:

```bash
cd projects/<project>/main/
git fetch upstream
git merge upstream/main
git push origin main
```

Rebase a scope branch onto updated main:

```bash
cd projects/<project>/<scope-name>/
git rebase main
```

## Common Operations

```bash
# Check which branch a worktree is on
git -C projects/<project>/<scope-name> branch --show-current

# View all branches
git -C projects/<project>/.bare branch -a

# Remove a worktree
git -C projects/<project>/.bare worktree remove <scope-name>

# Prune stale worktree references
git -C projects/<project>/.bare worktree prune
```
