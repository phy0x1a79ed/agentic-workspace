---
name: create-project
tags: [project, setup, initialization, bare-repo, clone, fork]
requires: []
description: Create new, forked, or cloned projects via awm CLI
---

# Create Project

## Commands

```bash
awm project create <project-name>                        # brand-new project
awm project create <project-name> --fork <upstream-url>  # fork on GitHub + clone as bare
awm project create <project-name> --clone <repo-url>     # clone existing repo as bare
```

All three modes scaffold: bare repo at `projects/<project>/.bare/`, default-branch worktree at `projects/<project>/<default-branch>/` (with an `AGENTS.md` rendered from the scope template), and data dirs under `data/<project>/`. No project-level AGENTS.md or data symlink is produced — per-scope `.awm/` directories carry all per-scope state.

## Post-Setup Verification

- Bare repo: `git -C projects/<project>/.bare rev-parse --is-bare-repository` returns `true`.
- Default-branch worktree exists at `projects/<project>/<default-branch>/` and is checked out to that branch.
- `data/<project>/raw/` and `data/<project>/staged/` exist.
- For `--fork` / `--clone`: `git -C projects/<project>/.bare remote -v` shows the expected `origin` (and `upstream` on fork).
- Initial commit is present on the default branch.
- `projects/<project>/<default-branch>/AGENTS.md` exists.

## Layout After Setup

```
projects/<project>/
  .bare/                   # bare repo
  <default-branch>/        # worktree checked out to the default branch
    AGENTS.md              # scope-agent scaffold

data/<project>/
  raw/                     # immutable inputs
  staged/                  # processed inputs
```
