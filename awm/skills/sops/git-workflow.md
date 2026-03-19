---
name: git-workflow
type: sop
tags: [git, bare-repo, worktree, branch, pr]
description: Bare repo + worktree model, branching, PRs
---

# Git Workflow

## Bare Repo + Worktree Model

Each project uses a **bare repository** with linked worktrees instead of a standard clone.

```
repos/<project>/
  .bare/             # bare repo (no working directory)
  {default_branch}/  # worktree -> default branch (e.g. main, release)
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

Branches are created automatically by `awm task create`.

## Listing Worktrees

```bash
git -C repos/<project>/.bare worktree list
```

## Creating Pull Requests

From within a task worktree:

```bash
cd repos/<project>/<task-name>/
git push -u origin feat/<task-name>
gh pr create --title "feat: <description>" --body "Summary of changes"
```

## Syncing with Upstream (Forked Repos)

Add the upstream remote (done automatically on fork setup):

```bash
git -C repos/<project>/.bare remote add upstream <upstream-url>
```

Fetch and merge upstream changes into the default branch:

```bash
cd repos/<project>/{default_branch}/
git fetch upstream
git merge upstream/{default_branch}
git push origin {default_branch}
```

Rebase a task branch onto updated default branch:

```bash
cd repos/<project>/<task-name>/
git rebase {default_branch}
```

## Common Operations

```bash
# Check which branch a worktree is on
git -C repos/<project>/<task-name> branch --show-current

# View all branches
git -C repos/<project>/.bare branch -a

# Remove a worktree
git -C repos/<project>/.bare worktree remove <task-name>

# Prune stale worktree references
git -C repos/<project>/.bare worktree prune
```
