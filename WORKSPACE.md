# Workspace Reference

Shared context for all agents in this workspace.

## Workspace Layout

| Path | Purpose |
|------|---------|
| `awm/` | AWM service package (Python) + skills catalog |
| `data/` | Shared data (reference + per-project raw/staged) |
| `repos/` | Project bare repos + git worktrees (clean code only) |
| `main/` | Agent workspaces (AGENTS.md, experiences, results, symlinks) |

### Per-Project Layout

```
main/{project}/
  data -> ../../data/{project}   # project-specific data (scoped, not global)
  tasks/                         # task workspaces live here
    {task}/                      # see Per-Task Layout below
```

### Per-Task Layout

```
repos/{project}/
  .bare/                         # bare git repo
  {task}/                        # git worktree — pure code, clean git status

main/{project}/tasks/{task}/     # agent workspace — AWM-managed, not a git repo
  AGENTS.md                      # task context (seeded at creation)
  experiences.md                 # session logs
  results/                       # task outputs
  repo -> ../../../../repos/{project}/{task}/   # symlink to git worktree
  skills -> {SKILLS_DIR}         # symlink to awm/skills/ (package data)
```

Tasks access project data via `../../data` (navigates up to the project-level data symlink).

## Task Lifecycle

1. **Create**: `task_create` sets up a git worktree on `feat/<task>`, an agent workspace with AGENTS.md/symlinks, and records the task in DB.
2. **Work**: Code in `repo/`. Write outputs to `results/`. Data is at `../../data`.
3. **Log**: `session_log` appends to `experiences.md` and records metadata in DB.
4. **Complete**: `task_complete` updates DB status, optionally merges branch with `--merge`.

## Git Model

Each project uses a **bare repo** at `repos/{project}/.bare/` with worktrees per task.

- Branch naming: `feat/<task>`, `fix/<task>`
- PRs created from feature branches
- For detailed git operations, see `skills/sops/git-workflow.md`

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use:** `python`, `python3`, `pip`, or `pip3` directly.
**Do NOT use:** `conda activate` or `mamba activate` (requires interactive shell init).

**Always use:**
```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

**Bootstrap** (if env doesn't exist):
```bash
mamba env list | grep -q <project-name> || mamba env create -f env/environment.yml
```

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write results to `results/`** — not in the repo code tree.
3. **Log sessions** at the end of each session via `session_log` — what you did, decisions made, gotchas, next steps.
4. **Don't duplicate data** — use symlinks. The workspace's `data/` and `skills/` dirs in task workspaces are symlinks.
5. **Reflect on completion** — when finishing a task, send a `reflection` message to your project scope (`inbox_send scope=project:{project}`) summarizing what worked well and problems encountered. Keep it brief and focused on lessons that help future tasks.

## Skills & Tools

SOPs, tool guides, and templates are in `skills/`. Use `skills_search` to find relevant guides before starting unfamiliar workflows.
