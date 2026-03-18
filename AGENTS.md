# Agentic Workspace

Universal entry point for CLI-based agents (Claude Code, Codex, OpenCode).

## Workspace Layout

| Path | Purpose | Git-tracked? |
|------|---------|-------------|
| `AGENTS.md` | This file — start here | Yes |
| `awm/` | AWM service package (Python) + skills catalog | Yes |
| `data/` | Shared data (reference + per-project raw/staged) | No |
| `repos/` | Project bare repos + git worktrees (clean code only) | No |
| `main/` | Agent workspaces (AGENTS.md, experiences, results, symlinks) | No |
| `.awm/` | Runtime state (SQLite DB, PID, logs) | No |
| `.mcp.json` | MCP server registration for Claude Code | Yes |

### Per-Task Layout

```
repos/{project}/
  .bare/                         # bare git repo
  {task}/                        # git worktree — pure code, clean git status

main/{project}/{task}/           # agent workspace — AWM-managed, not a git repo
  AGENTS.md                      # task context (seeded at creation)
  experiences.md                 # session logs
  results/                       # task outputs (replaces workspace-level results/)
  inbox/                         # placeholder for inter-task messaging
  repo -> ../../repos/{project}/{task}/   # symlink to git worktree
  skills -> {SKILLS_DIR}         # symlink to awm/skills/ (package data)
  data -> ../../../data/         # symlink to workspace data
```

## Quick Start

AWM (Agentic Workspace Manager) provides a unified CLI for all workspace operations. The server auto-starts on first use.

```bash
# List active tasks
awm task list

# Create a new project
awm project create <name> [--clone <url>] [--fork <url>]

# Create a task within a project (with optional context seeding)
awm task create <project> <task> [--from <branch>] [--context "task brief"]

# Complete a task
awm task complete <project> <task> [--merge]

# Log a session
awm session log <project> <task> --summary "What was accomplished"

# Search skills
awm skill search <query>
```

## MCP Integration

AWM is also available as an MCP server for direct tool use by Claude Code. The `.mcp.json` at the workspace root registers the `awm` MCP server, which exposes 18 tools:

| Category | Tools |
|----------|-------|
| Skills | `skills_list`, `skills_get`, `skills_search`, `skills_reindex` |
| Sessions | `session_log`, `session_list`, `session_get`, `session_reflect` |
| Tasks | `task_create`, `task_list`, `task_complete`, `task_update` |
| Projects | `project_create` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Status | `awm_status` |

When working in this workspace, prefer MCP tools for programmatic access and the CLI for interactive use.

## Skill Discovery

Use the AWM skill commands to browse and search the skills catalog:

```bash
awm skill list                        # all skills with metadata
awm skill list --type sop             # filter by type
awm skill search git                  # search by keyword
awm skill get sops/git-workflow.md    # read a specific skill
awm skill reindex                     # regenerate skills/_index.md
```

Or read `awm/skills/_index.md` directly for a full catalog.

## Task Lifecycle

1. **Create**: `awm task create <project> <task>` creates a git worktree at `repos/<project>/<task>/` on `feat/<task>`, an agent workspace at `main/<project>/<task>/` with AGENTS.md, symlinks, and directories, and records the task in DB.
2. **Work**: Do analysis in the workspace. Write outputs to `main/<project>/<task>/results/`. Data is at `data/` via symlink. Commit code to the feature branch in the `repo/` symlink.
3. **Log**: `awm session log <project> <task> --summary "..."` appends to `experiences.md` in the workspace and records metadata in the DB.
4. **Complete**: `awm task complete <project> <task>` updates DB status, optionally merges branch with `--merge`.

## Session Logging

Session logs follow a DB + file pattern:
- **SQLite** (metadata index): project, task, summary, agent_id, timestamp
- **Files** (content): `experiences.md` in each task's workspace (`main/`) holds the full entries

```bash
awm session log myproject analysis \
  --summary "Completed normalization pipeline" \
  --decision "Used quantile normalization" \
  --issue "Missing values in batch 3" \
  --next-step "Validate with PCA" \
  --agent agent1

awm session list --project myproject
awm session get <id>
awm session reflect --query "normalization"
```

## Git Model

Each project uses a **bare repo** at `repos/{project}/.bare/` with worktrees for each task. Git worktrees contain only code — no AWM artifacts pollute `git status`.

- Branch naming: `feat/<task>`, `fix/<task>`
- PRs created from feature branches
- See `awm/skills/sops/git-workflow.md` for details

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
2. **Write results to `main/{project}/{task}/results/`** — not in the repo code tree.
3. **Log sessions via `awm session log`** at the end of each session — what you did, decisions made, gotchas, next steps.
4. **Read skills** before starting unfamiliar workflows — use `awm skill search` to find relevant SOPs.
5. **Don't duplicate data** — use symlinks. The workspace's `data/` and `skills/` dirs in task workspaces are symlinks.

## Existing Projects

| Project | Source | Default Branch | Upstream |
|---------|--------|---------------|----------|
| metasmith | phy0x1a79ed/Metasmith | release | hallamlab/Metasmith |
| metasmith-libraries | phy0x1a79ed/MetasmithLibraries | main | hallamlab/MetasmithLibraries |
| cyanoverse | phy0x1a79ed/cyanoverse | main | — |
| self-improvement | local | main | — |
