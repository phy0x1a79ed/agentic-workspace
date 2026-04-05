# Agentic Workspace Manager (AWM)

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/scope management, file locking with crash recovery, skills catalog, session logging, experience tracking, artifact registration, inter-agent messaging, autonomous agent spawning, and an MCP server for direct tool use by Claude Code.

AWM supports a **two-level agent hierarchy** — a workspace agent that triages and delegates, and scope agents that do the actual work. See [Agent System](#agent-system) for details.

## Quick Install

```bash
./setup.sh
```

This creates an `awm` mamba environment, installs the package, initializes the database, and adds `awm` and `awm-mcp` to your PATH.

## Manual Install

```bash
mamba env create -f environment.yml
mamba run -n awm pip install -e . --no-deps
mkdir -p .awm
mamba run -n awm python -m awm init
```

## Usage

### Server

The server auto-starts when you run any CLI command. To run manually:

```bash
awm serve          # foreground
awm status         # health check (auto-starts if needed)
awm stop           # stop the server
awm refresh        # restart server to pick up source changes
```

### Projects

```bash
awm project create myproject
awm project create myproject --clone https://github.com/org/repo.git
awm project create myproject --fork https://github.com/org/repo.git
```

### Scopes

Scopes are isolated git worktrees where agents do their work.

```bash
awm scope create myproject analysis-v1
awm scope create myproject analysis-v1 --from develop
awm scope list
awm scope list --status active --project myproject
awm scope complete myproject analysis-v1
awm scope complete myproject analysis-v1 --merge
awm scope delete myproject analysis-v1
```

### Skills

```bash
awm skill list                              # list all skills with metadata
awm skill list --type protocol              # filter by type
awm skill list --tags git,workflow           # filter by tags
awm skill get tools/git.md                  # read a skill file
awm skill search "HPC annotation"           # hybrid keyword + semantic search
awm skill reindex                           # regenerate index + embeddings
```

### Session Logging

```bash
# Log a session
awm session log myproject analysis-v1 \
  --summary "Completed normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1

# Query sessions
awm session list --project myproject
awm session get 42
```

### Locking

```bash
# Acquire a file lock
awm lock acquire data/myproject/raw/sample.csv --holder agent1

# Acquire a folder lock (trailing slash)
awm lock acquire data/myproject/ --holder agent1 --type exclusive

# Shared lock (multiple readers)
awm lock acquire data/reference/genome.fa --holder agent1 --type shared
awm lock acquire data/reference/genome.fa --holder agent2 --type shared

# Release
awm lock release data/myproject/ --holder agent1

# List and reap
awm lock list
awm lock list --holder agent1
awm lock reap
```

### Shared Resource Edits

For editing tracked files in the outer repo (AGENTS.md, awm/ package files):

```bash
awm shared edit --name update-git-sop --by agent1
# Work in the shared edit worktree, commit changes
awm shared merge --name update-git-sop
awm shared list
```

## Agent System

AWM implements a two-level agent hierarchy defined by AGENTS.md persona content. The workspace agent orchestrates while scope agents execute.

### Levels

| Level | Directory | Role |
|-------|-----------|------|
| **Workspace** | Workspace root | Triage, route, delegate, monitor — never does implementation work |
| **Scope** | `projects/{project}/{scope}/` | Executes work in an isolated git worktree |

### Messaging

Agents communicate via scoped message queues stored in SQLite:

```bash
# Send a message (via MCP tool)
inbox_send scope=project:myproject sender=workspace msg_type=task_assignment subject="New work" body="..."

# Check inbox
inbox_search scope=workspace                    # all workspace messages
inbox_search scope=scope:myproject/analysis-v1   # scope-specific messages
inbox_search status=unread                       # only unread
inbox_search msg_type=reflection                 # filter by type

# Mark as read
inbox_read id=42

# List valid recipients
inbox_recipients
```

**Message types**: `task_assignment`, `reflection`, `status_update`, `notification`, `plan`

### Agent Spawning

The workspace agent can delegate work by spawning fire-and-forget agent subprocesses:

```bash
# Spawn an agent on a scope (via MCP tool)
agent_spawn project=myproject scope=analysis-v1 prompt="Implement feature X"

# The prompt is automatically sent to the scope's inbox as a 'plan' message
# The agent runs detached, logging output to the scope worktree
```

Supported CLIs: `opencode` (default, interactive TUI) and `claude` (non-interactive `--print` mode). The default is configurable via the `agent_cli` key in the config table.

The 3-level agent hierarchy (workspace / project / scope) is documented in the workspace-level `AGENTS.md`.

## MCP Server

AWM includes an MCP (Model Context Protocol) server for direct integration with Claude Code and other MCP clients. It exposes 27 tools covering skills, sessions, scopes, experiences, artifacts, projects, locks, messaging, agents, and status.

### Setup

The `.mcp.json` at the workspace root registers the server:

```json
{
  "mcpServers": {
    "awm": {
      "command": "mamba",
      "args": ["run", "-n", "awm", "awm-mcp"],
      "env": { "AWM_WORKSPACE": "/home/tony/agentic_workspace" }
    }
  }
}
```

Claude Code automatically discovers this file and connects to the MCP server.

### Running Manually

```bash
awm-mcp    # starts stdio MCP server (used by MCP clients, not interactive)
```

### Tools

| Category | Tools |
|----------|-------|
| Skills | `skills_list`, `skills_get`, `skills_search`, `skills_reindex` |
| Sessions | `session_log`, `session_list`, `session_get` |
| Scopes | `scope_create`, `scope_list`, `scope_complete`, `scope_delete` |
| Experiences | `experience_log`, `experience_list` |
| Artifacts | `artifact_register`, `artifact_search` |
| Projects | `project_create` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Messaging | `inbox_send`, `inbox_search`, `inbox_read`, `inbox_recipients` |
| Agents | `agent_spawn` |
| Status | `awm_status`, `awm_refresh` |

## REST API

The server listens on `127.0.0.1:7819`. Key endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Health + summary |
| POST | `/projects` | Create project |
| GET | `/scopes` | List scopes |
| POST | `/scopes` | Create scope |
| PATCH | `/scopes/{project}/{scope}` | Update scope (complete) |
| DELETE | `/scopes/{project}/{scope}` | Delete scope |
| POST | `/locks` | Acquire lock |
| DELETE | `/locks` | Release lock |
| GET | `/locks` | List locks |
| POST | `/locks/heartbeat` | Renew heartbeat |
| POST | `/locks/reap` | Force reap stale locks |
| POST | `/shared` | Start shared edit |
| POST | `/shared/{name}/merge` | Merge shared edit |
| GET | `/shared` | List shared edits |
| GET | `/skills` | List skills (query: `type`, `tags`) |
| GET | `/skills/search` | Search skills (query: `q`) |
| GET | `/skills/{path}` | Get skill content |
| POST | `/skills/reindex` | Regenerate skills index |
| POST | `/sessions` | Log a session entry |
| GET | `/sessions` | List session logs (query: `project`, `scope`, `limit`) |
| GET | `/sessions/{id}` | Get session with full content |

## Locking Protocol

1. **Acquire** before accessing shared resources
2. **Heartbeat** every 30 seconds (`POST /locks/heartbeat?holder=<id>`)
3. **Release** when done
4. **Stale locks** are automatically reaped after 120s without heartbeat, or immediately if the holder PID is gone
5. **Folder locks** (path ending with `/`) cover everything beneath that path
6. **Shared locks** allow multiple concurrent readers; exclusive locks block all others

## Architecture

```
                  ┌─────────┐  ┌──────────┐  ┌───────────┐
                  │ Typer   │  │ FastAPI  │  │ MCP stdio │
                  │ CLI     │  │ HTTP     │  │ Server    │
                  └────┬────┘  └────┬─────┘  └─────┬─────┘
                       │            │              │
                       └────────────┼──────────────┘
                                    │
                           ┌────────▼────────┐
                           │  awm/services/  │
                           │  (shared core)  │
                           └────────┬────────┘
                                    │
                        ┌───────────┼───────────┐
                        │           │           │
                   ┌────▼───┐ ┌────▼────┐ ┌────▼────┐
                   │ SQLite │ │  Files  │ │  Git    │
                   │ (index)│ │(content)│ │(history)│
                   └────────┘ └─────────┘ └─────────┘
```

```
awm/                      # Git-tracked Python package
  __init__.py
  __main__.py             # Entry point (python -m awm)
  cli.py                  # Typer CLI
  server.py               # FastAPI + uvicorn
  mcp_server.py           # MCP stdio server
  config.py               # Paths and settings
  db.py                   # SQLite (WAL mode) + migrations
  models.py               # Pydantic models
  services/
    projects.py           # Project CRUD
    scopes.py             # Scope CRUD (worktrees)
    locks.py              # Lock management
    skills.py             # Skills scanning + index generation
    sessions.py           # Session log CRUD (DB + file + git)
    experiences.py        # Experience logging (execution traces)
    artifacts.py          # Artifact registration + search
    embeddings.py         # Sentence-transformer embeddings
    shared_resources.py   # Outer-repo worktree flow
    messaging.py          # Scoped message queues (inbox)
    agents.py             # Fire-and-forget agent spawning
    config_service.py     # Key-value config store
.awm/                     # Runtime state (gitignored)
  state.db                # SQLite database (schema v11)
  awm.pid                 # Server PID
  awm.log                 # Server log
.mcp.json                 # MCP server registration
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var).

## Troubleshooting

**Port in use**: `awm stop` then retry, or `lsof -i :7819` to find the process.

**Stale locks**: `awm lock reap` forces cleanup. The reaper also runs automatically every 30s.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Delete `.awm/state.db` and run `awm init` to recreate (schema v11).

**MCP not connecting**: Verify `.mcp.json` exists at workspace root. Check that `awm-mcp` is on PATH (`mamba run -n awm which awm-mcp`). Restart Claude Code to pick up changes.
