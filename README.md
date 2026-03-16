# Agentic Workspace Manager (AWM)

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/task management, file locking with crash recovery, and shared resource versioning through git worktrees.

## Quick Install

```bash
./setup.sh
```

This creates a `awm` mamba environment, installs the package, initializes the database, and adds `awm` to your PATH.

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
awm task create myproject analysis-v1 --from develop
awm task list
awm task list --status active --project myproject
awm task pause myproject analysis-v1
awm task resume myproject analysis-v1
awm task complete myproject analysis-v1
awm task complete myproject analysis-v1 --merge
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

For editing tracked files in the outer repo (skills/, scripts/, AGENTS.md):

```bash
awm shared edit --name update-git-sop --by agent1
# Work in tasks/_shared/update-git-sop/, commit changes
awm shared merge --name update-git-sop
awm shared list
```

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

### curl Examples

```bash
# Health check
curl localhost:7819/status

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
awm/                  # Git-tracked Python package
  cli.py              # Typer CLI
  server.py           # FastAPI + uvicorn
  config.py           # Paths and settings
  db.py               # SQLite (WAL mode)
  models.py           # Pydantic models
  services/
    projects.py       # Project CRUD
    tasks.py          # Task CRUD
    locks.py          # Lock management
    shared_resources.py  # Outer-repo worktree flow
.awm/                 # Runtime state (gitignored)
  state.db            # SQLite database
  awm.pid             # Server PID
  awm.log             # Server log
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var).

## Troubleshooting

**Port in use**: `awm stop` then retry, or `lsof -i :7819` to find the process.

**Stale locks**: `awm lock reap` forces cleanup. The reaper also runs automatically every 30s.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Delete `.awm/state.db` and run `awm init` to recreate.
