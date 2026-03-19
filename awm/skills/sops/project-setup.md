---
name: project-setup
type: sop
tags: [project, setup, initialization, bare-repo]
description: Creating new, forked, or cloned projects via awm CLI
---

# Project Setup

## Creating a New Project

```bash
awm project create <project-name>
```

This creates the full project scaffold: bare repo at `repos/<project>/.bare/`, default branch worktree, data directories, agent workspace, and GitHub remote.

## Forking an Existing Project

```bash
awm project create <project-name> --fork <upstream-url>
```

Forks the upstream repo on GitHub, clones as a bare repo, and sets up worktrees with upstream tracking.

## Cloning an Existing Project

```bash
awm project create <project-name> --clone <repo-url>
```

Clones into bare repo format and creates the default branch worktree.

## Post-Setup Checklist

After creating the project, verify:

- [ ] **Bare repo exists**: `repos/<project>/.bare/` is a valid bare repo (`git -C <path> rev-parse --is-bare-repository` returns `true`)
- [ ] **Default branch worktree**: `repos/<project>/{default_branch}/` exists and is on the correct branch
- [ ] **Data directories**: `data/<project>/raw/` and `data/<project>/staged/` exist
- [ ] **GitHub remote**: `git -C repos/<project>/.bare remote -v` shows the correct GitHub URL
- [ ] **Agent workspace**: `main/<project>/` exists with `data` symlink and `tasks/` directory
- [ ] **Initial commit**: Default branch has at least one commit

## Directory Structure After Setup

```
repos/<project>/
  .bare/                 # bare repo
  {default_branch}/      # default branch worktree (e.g. main, release)

main/<project>/
  data -> ../../data/<project>   # project data symlink
  tasks/                         # task workspaces created here

data/<project>/
  raw/
  staged/
```
