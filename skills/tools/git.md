---
name: git
tags: [git, bare-repo, worktree, branch, pr, merge]
requires: []
description: Bare repo + worktree model, branching, PRs, upstream sync
---

# Git

## Layout

Each project is a **bare repository** with per-scope worktrees — no shared working directory, no branch switching.

```
projects/<project>/
  .bare/              # bare repo
  main/               # worktree -> main branch (or `release` for forks)
  <scope>/            # worktree -> feat/<scope> branch
```

Branches are created automatically by `awm scope create` as `feat/<scope>` (or `fix/<scope>` when the work is a bugfix — rename the branch manually if needed).

## Listing worktrees

```bash
git -C projects/<project>/.bare worktree list
```

## Pull request

```bash
cd projects/<project>/<scope>/
git push -u origin feat/<scope>
gh pr create --title "feat: <description>" --body "Summary of changes"
```

## Upstream sync (forks)

Upstream remote is set up automatically on `awm project create --fork`. To manually refresh:

```bash
cd projects/<project>/main/
git fetch upstream
git merge upstream/main
git push origin main
```

Rebase a scope branch onto updated main:

```bash
cd projects/<project>/<scope>/
git rebase main
```

## Housekeeping

```bash
# Remove a finished worktree
git -C projects/<project>/.bare worktree remove <scope>

# Prune stale worktree references (after a manual rm -rf)
git -C projects/<project>/.bare worktree prune

# Show all branches
git -C projects/<project>/.bare branch -a
```
