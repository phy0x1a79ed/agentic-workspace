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

This creates the full project scaffold: bare repo at `tasks/<project>/.bare/`, main worktree, data directories, and GitHub remote.

## Forking an Existing Project

```bash
awm project create <project-name> --fork <upstream-url>
```

Forks the upstream repo on GitHub, clones as a bare repo, and sets up worktrees with upstream tracking.

## Cloning an Existing Project

```bash
awm project create <project-name> --clone <repo-url>
```

Clones into bare repo format and creates the main worktree.

## Post-Setup Checklist

After creating the project, verify:

- [ ] **Bare repo exists**: `tasks/<project>/.bare/` is a valid bare repo (`git -C <path> rev-parse --is-bare-repository` returns `true`)
- [ ] **Main worktree**: `tasks/<project>/main/` exists and is on the `main` branch
- [ ] **Data directories**: `data/<project>/raw/` and `data/<project>/staged/` exist
- [ ] **GitHub remote**: `git -C tasks/<project>/.bare remote -v` shows the correct GitHub URL
- [ ] **Results + reports**: `results/<project>/` and `reports/<project>/` exist
- [ ] **Initial commit**: Main branch has at least one commit

## Directory Structure After Setup

```
tasks/<project>/
  .bare/                 # bare repo
  main/                  # main worktree (checked out to main branch)

data/<project>/
  raw/
  staged/

results/<project>/
reports/<project>/
```
