---
name: create-project
tags: [project, setup, initialization, bare-repo, clone, fork]
requires: []
description: Create new, forked, or cloned projects via awm CLI
---

# Create Project

## Commands

```bash
awm scope create --project <project-name> --new-project                         # brand-new project
awm scope create --project <project-name> --new-project --fork-url <upstream-url>  # fork on GitHub + clone as bare
awm scope create --project <project-name> --new-project --clone-url <repo-url>     # clone existing repo as bare
```

Without `--scope`, `scope create` makes the project alone. All three modes scaffold: bare repo at `projects/<project>/.bare/`, a default-branch worktree at `projects/<project>/<default-branch>/` fully scaffolded like a scope (a gitignored `.awm/` metadata dir + an `agents` DB row), and data dirs under `data/<project>/`. The default-branch worktree is treated as the project's first scope, so it appears in `scope search` immediately — no follow-up `scope repair` is needed. No tracked `AGENTS.md` is written into the worktree (that would dirty a freshly cloned repo); all per-scope state lives in the gitignored `.awm/`.

## Post-Setup Verification

- Bare repo: `git -C projects/<project>/.bare rev-parse --is-bare-repository` returns `true`.
- Default-branch worktree exists at `projects/<project>/<default-branch>/` and is checked out to that branch.
- `projects/<project>/<default-branch>/.awm/` exists with `context.md`, `history.md`, and `data`/`skills` symlinks.
- `data/<project>/raw/` and `data/<project>/staged/` exist.
- For `--fork-url` / `--clone-url`: `git -C projects/<project>/.bare remote -v` shows the expected `origin` (and `upstream` on fork).
- Initial commit is present on the default branch (fresh mode).
- `awm scope search` with no `--project` lists the new project.

## Layout After Setup

```
projects/<project>/
  .bare/                   # bare repo
  <default-branch>/        # worktree checked out to the default branch
    .awm/                  # gitignored scope metadata (context/history + symlinks)

data/<project>/
  raw/                     # immutable inputs
  staged/                  # processed inputs
```
