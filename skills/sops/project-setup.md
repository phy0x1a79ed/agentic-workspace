---
name: project-setup
type: sop
tags: [project, setup, initialization, bare-repo]
---

# Project Setup

## Creating a New Project

```bash
scripts/new-project.sh <project-name>
```

This creates the full project scaffold: bare repo, main worktree, data directories, and GitHub remote.

## Forking an Existing Project

```bash
scripts/new-project.sh --fork <upstream-url> <project-name>
```

Forks the upstream repo on GitHub, clones as a bare repo, and sets up worktrees with upstream tracking.

## Cloning an Existing Project

```bash
scripts/new-project.sh --clone <repo-url> <project-name>
```

Clones into bare repo format and creates the main worktree.

## Post-Setup Checklist

After running `scripts/new-project.sh`, verify:

- [ ] **Bare repo exists**: `projects/<project-name>/<project-name>.git/` is a valid bare repo (`git -C <path> rev-parse --is-bare-repository` returns `true`)
- [ ] **Main worktree**: `projects/<project-name>/main/` exists and is on the `main` branch
- [ ] **Data directories**: `projects/<project-name>/data/` structure is present (raw, processed, external subdirs)
- [ ] **GitHub remote**: `git -C <bare-repo> remote -v` shows the correct GitHub URL
- [ ] **Mamba env**: `envs/environment.yml` exists in the main worktree if dependencies are needed
- [ ] **Initial commit**: Main branch has at least one commit

## Directory Structure After Setup

```
projects/<project-name>/
  <project-name>.git/    # bare repo
  main/                  # main worktree (checked out to main branch)
  data/
    raw/
    processed/
    external/
```
