---
name: git-workflow
type: sop
tags: [git, bare-repo, worktree, branch, pr]
---

# Git Workflow

## Bare Repo + Worktree Model

Each project uses a **bare repository** with linked worktrees instead of a standard clone.

```
projects/<project>/
  <project>.git/     # bare repo (no working directory)
  main/              # worktree -> main branch
  <task-name>/       # worktree -> feat/<task-name> branch
```

### Why Bare Repos

- **No checkout conflicts**: A bare repo has no working directory, so switching branches in one worktree never affects another.
- **Parallel work**: Multiple worktrees can be checked out to different branches simultaneously.
- **Clean separation**: Each task gets its own directory with its own branch; no stashing or context switching required.

## Branch Naming

| Prefix          | Use case                        |
|-----------------|---------------------------------|
| `feat/<task>`   | New features or enhancements    |
| `fix/<task>`    | Bug fixes                       |

Branches are created automatically by `scripts/new-task.sh`.

## Listing Worktrees

```bash
git -C projects/<project>/<project>.git worktree list
```

## Creating Pull Requests

From within a task worktree:

```bash
cd projects/<project>/<task-name>/
git push -u origin feat/<task-name>
gh pr create --title "feat: <description>" --body "Summary of changes"
```

## Syncing with Upstream (Forked Repos)

Add the upstream remote (done automatically on fork setup):

```bash
git -C projects/<project>/<project>.git remote add upstream <upstream-url>
```

Fetch and merge upstream changes into main:

```bash
cd projects/<project>/main/
git fetch upstream
git merge upstream/main
git push origin main
```

Rebase a task branch onto updated main:

```bash
cd projects/<project>/<task-name>/
git rebase main
```

## Common Operations

```bash
# Check which branch a worktree is on
git -C projects/<project>/<task-name> branch --show-current

# View all branches
git -C projects/<project>/<project>.git branch -a

# Remove a worktree
git -C projects/<project>/<project>.git worktree remove <task-name>

# Prune stale worktree references
git -C projects/<project>/<project>.git worktree prune
```
