# Agentic Workspace

Universal entry point for CLI-based agents (Claude Code, Codex, OpenCode).

## Workspace Layout

| Path | Purpose | Git-tracked? |
|------|---------|-------------|
| `AGENTS.md` | This file — start here | Yes |
| `skills/` | SOPs, tool guides, templates | Yes |
| `scripts/` | Workspace management scripts | Yes |
| `data/` | Shared data (reference + per-project raw/staged) | No |
| `results/` | Per-project per-task analysis outputs | No |
| `reports/` | Report packages (PDFs, viz, tables) | No |
| `tasks/` | Project repos (bare) + task worktrees | No |
| `tasks_active/` | Symlinks to active task worktrees | No |

## Quick Start

```bash
# List active tasks
./scripts/list-tasks.sh

# Create a new project
./scripts/new-project.sh <name> [--clone <url>] [--fork <url>]

# Create a task within a project
./scripts/new-task.sh <project> <task> [--from <branch>]

# Complete a task
./scripts/complete-task.sh <project> <task> [--merge]
```

## Skill Discovery

Read `skills/_index.md` for a catalog of all available SOPs, tool guides, and templates. Skills are plain markdown with YAML frontmatter — any agent can read them.

## Task Lifecycle

1. **Create**: `new-task.sh` creates a git worktree on `feat/<task>`, sets `.status=active`, symlinks shared dirs, adds to `tasks_active/`.
2. **Work**: Do analysis in the worktree. Write outputs to `results/<project>/<task>/`. Data is at `data/` via symlink. Commit code to the feature branch.
3. **Pause**: Set `.status=paused` manually if needed. Worktree stays in place.
4. **Complete**: `complete-task.sh` sets `.status=completed`, removes from `tasks_active/`, optionally merges branch.

### .status conventions
- `active` — work in progress
- `paused` — on hold, will resume
- `completed` — done, kept for reference

### tasks_active/
Quick-access view of active work. Each symlink is named `{project}_{task}` and points to the worktree.

## Git Model

Each project uses a **bare repo** at `tasks/{project}/.bare/` with worktrees for each task. This avoids checkout conflicts and allows parallel work.

- Branch naming: `feat/<task>`, `fix/<task>`
- PRs created from feature branches
- See `skills/sops/git-workflow.md` for details

## Environment Convention

- **Per-project**: mamba env named after the project, defined in `envs/environment.yml` in the project repo
- **Per-task overlay**: `env/environment.yml` in the worktree, installed with `mamba env update -n <project> --file env/environment.yml`
- Channel priority: `conda-forge` > `bioconda`

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write results to `results/{project}/{task}/`** — not in the worktree code tree.
3. **Append to `experiences.md`** at the end of each session — what you did, decisions made, gotchas, next steps.
4. **Update `.status`** when task state changes.
5. **Read `skills/`** before starting unfamiliar workflows — SOPs exist for common operations.
6. **Don't duplicate data** — use symlinks. The worktree's `data/`, `results/`, `reports/` are symlinks to workspace-level dirs.

## Existing Projects

Projects at `/home/tony/workspace/` (cyanoverse, MSM, etc.) are external. Reference them but don't migrate.
