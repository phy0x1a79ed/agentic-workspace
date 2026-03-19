# Agentic Workspace Manager (AWM)

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/task management, file locking with crash recovery, skills catalog, session logging, inter-agent messaging, autonomous agent spawning, and an MCP server for direct tool use by Claude Code.

AWM supports a **three-level agent hierarchy** — workspace, project, and task agents — each with specialized personas, startup rituals, and communication patterns. See [Agent System](#agent-system) for details.

## Quick Install

```bash
./setup.sh
```

This creates a `awm` mamba environment, installs the package, initializes the database, and adds `awm` and `awm-mcp` to your PATH.

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

### Tasks

```bash
awm task create myproject analysis-v1
awm task create myproject analysis-v1 --from dev
awm task list
awm task list --status active --project myproject
awm task pause myproject analysis-v1
awm task resume myproject analysis-v1
awm task complete myproject analysis-v1
awm task complete myproject analysis-v1 --merge
```

### Skills

```bash
awm skill list                              # list all skills with metadata
awm skill list --type sop                   # filter by type (sop, tool, template)
awm skill list --tags git,workflow           # filter by tags
awm skill get sops/git-workflow.md          # read a skill file
awm skill search normalization              # search by keyword
awm skill reindex                           # regenerate awm/skills/_index.md
```

### Session Logging

```bash
# Log a session (records in DB)
awm session log myproject analysis-v1 \
  --summary "Completed normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1

# Query sessions
awm session list --project myproject
awm session list --project myproject --task analysis-v1
awm session get 42

# Reflect across past sessions
awm session reflect --query "normalization"
awm session reflect --project myproject
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
# Work in tasks/_shared/update-git-sop/, commit changes
awm shared merge --name update-git-sop
awm shared list
```

## Agent System

AWM implements a three-level agent hierarchy where each level is defined by AGENTS.md persona content (not standalone SDK agents). The personas shape how Claude Code / opencode behaves at each directory level.

### Levels

| Level | Directory | Role |
|-------|-----------|------|
| **Workspace** | Workspace root | Triage, route, delegate, monitor — never does implementation work |
| **Project** | `main/{project}/` | Manages tasks within a project, coordinates, reports status up |
| **Task** | `main/{project}/tasks/{task}/` | Executes the plan from inbox, does the actual work |

### Messaging

Agents communicate via scoped message queues stored in SQLite:

```bash
# Send a message (via MCP tool)
inbox_send scope=project:myproject sender=workspace msg_type=task_assignment subject="New work" body="..."

# Check inbox
inbox_search scope=workspace                    # all workspace messages
inbox_search scope=task:myproject/analysis-v1    # task-specific messages
inbox_search status=unread                       # only unread
inbox_search msg_type=reflection                 # filter by type

# Mark as read
inbox_read id=42

# List valid recipients
inbox_recipients
```

**Message types**: `task_assignment`, `reflection`, `status_update`, `notification`, `plan`

**Scopes**: `workspace`, `project:{name}`, `task:{project}/{task}`

### Agent Spawning

The workspace agent can delegate work by spawning fire-and-forget agent subprocesses:

```bash
# Spawn an agent on a task (via MCP tool)
agent_spawn project=myproject task=analysis-v1 prompt="Implement feature X"

# The prompt is automatically sent to the task's inbox as a 'plan' message
# The agent runs detached, logging output to main/{project}/tasks/{task}/agent.log
```

Supported CLIs: `opencode` (default, interactive TUI) and `claude` (non-interactive `--print` mode). The default is configurable via the `agent_cli` key in the config table.

### SOPs

- `awm skill get sops/agent-personas` — full persona SOP (startup rituals, triage rules, delegation protocol)
- `awm skill get sops/agent-spawning` — spawning details, CLI config, inbox protocol

## MCP Server

AWM includes an MCP (Model Context Protocol) server for direct integration with Claude Code and other MCP clients. It exposes 23 tools covering skills, sessions, tasks, projects, locks, messaging, agents, and status.

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
| Sessions | `session_log`, `session_list`, `session_get`, `session_reflect` |
| Tasks | `task_create`, `task_list`, `task_complete`, `task_delete` |
| Projects | `project_create` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Messaging | `inbox_send`, `inbox_search`, `inbox_read`, `inbox_recipients` |
| Agents | `agent_spawn` |
| Status | `awm_status` |

## REST API

The server listens on `127.0.0.1:7819`. All endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Health + summary |
| POST | `/projects` | Create project |
| GET | `/tasks` | List tasks |
| POST | `/tasks` | Create task |
| PATCH | `/tasks/{project}/{task}` | Update task (complete/pause/resume) |
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
| GET | `/sessions` | List session logs (query: `project`, `task`, `limit`) |
| GET | `/sessions/{id}` | Get session with full content |
| GET | `/sessions/reflect` | Search sessions (query: `project`, `task`, `q`) |

### curl Examples

```bash
# Health check
curl localhost:7819/status

# List skills
curl localhost:7819/skills
curl 'localhost:7819/skills?type=sop'
curl 'localhost:7819/skills/search?q=git'

# Get a skill
curl localhost:7819/skills/sops/git-workflow.md

# Log a session
curl -X POST localhost:7819/sessions \
  -H 'Content-Type: application/json' \
  -d '{"project":"myproject","task":"analysis","summary":"Did things","agent_id":"agent1"}'

# List sessions
curl 'localhost:7819/sessions?project=myproject'

# Reflect across sessions
curl 'localhost:7819/sessions/reflect?q=normalization'

# Create task
curl -X POST localhost:7819/tasks \
  -H 'Content-Type: application/json' \
  -d '{"project":"myproject","task":"analysis"}'

# Acquire lock
curl -X POST localhost:7819/locks \
  -H 'Content-Type: application/json' \
  -d '{"resource_path":"data/myproject/","holder_id":"agent1","lock_type":"exclusive"}'

# Heartbeat (agents should call every 30s)
curl -X POST 'localhost:7819/locks/heartbeat?holder=agent1'

# Release lock
curl -X DELETE 'localhost:7819/locks?path=data/myproject/&holder=agent1'
```

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
    tasks.py              # Task CRUD
    locks.py              # Lock management
    skills.py             # Skills scanning + index generation
    sessions.py           # Session log CRUD (DB-only storage)
    shared_resources.py   # Outer-repo worktree flow
    messaging.py          # Scoped message queues (inbox)
    agents.py             # Fire-and-forget agent spawning
    config_service.py     # Key-value config store
.awm/                     # Runtime state (gitignored)
  state.db                # SQLite database (schema v9)
  awm.pid                 # Server PID
  awm.log                 # Server log
.mcp.json                 # MCP server registration
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var).

## Troubleshooting

**Port in use**: `awm stop` then retry, or `lsof -i :7819` to find the process.

**Stale locks**: `awm lock reap` forces cleanup. The reaper also runs automatically every 30s.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Delete `.awm/state.db` and run `awm init` to recreate (schema v9).

**MCP not connecting**: Verify `.mcp.json` exists at workspace root. Check that `awm-mcp` is on PATH (`mamba run -n awm which awm-mcp`). Restart Claude Code to pick up changes.
