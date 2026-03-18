---
name: Agent Spawning
type: sop
tags: [agents, spawning, delegation, cli]
description: How to spawn task agents, configure CLIs, and manage the fire-and-forget lifecycle
---

# Agent Spawning SOP

## Overview

The workspace agent can delegate work by spawning fire-and-forget agent subprocesses on task workspaces. Each spawned agent runs in its own terminal session with access to the task's `AGENTS.md`, inbox, and code.

## Spawning an Agent

Use the `agent_spawn` MCP tool:

```
agent_spawn project=<project> task=<task> [prompt="..."] [agent_cli=opencode|claude]
```

### What Happens

1. The CLI is resolved: explicit `agent_cli` param → config table `agent_cli` key → default `opencode`
2. The task workspace is verified at `main/{project}/tasks/{task}/`
3. If a prompt is provided, it's sent as a `plan` message to `task:{project}/{task}` inbox
4. The agent process is spawned with `start_new_session=True` (detached)
5. Output is logged to `main/{project}/tasks/{task}/agent.log`

### CLI Options

| CLI | Command | Notes |
|-----|---------|-------|
| `opencode` | `opencode` in task cwd | Interactive TUI — agent reads AGENTS.md on startup |
| `claude` | `claude --print -p <prompt>` in task cwd | Non-interactive, prints result and exits |

### Configuring the Default CLI

The default CLI is stored in the `config` table:

```sql
-- Check current default
SELECT value FROM config WHERE key = 'agent_cli';

-- Change default (via service layer)
-- config_service.set_config('agent_cli', 'claude')
```

## Inbox Protocol

### Before Spawning

1. Create the task if it doesn't exist: `task_create project=X task=Y`
2. Optionally send context: the `prompt` parameter handles this automatically

### After Spawning

The spawned agent should:
1. Check inbox on startup: `inbox_search scope=task:{project}/{task}`
2. Read and acknowledge the plan: `inbox_read id=N`
3. Execute the work
4. Send a `reflection` message to `project:{project}` on completion
5. Log the session: `session_log`

### Monitoring

- Check `agent.log` in the task workspace for raw output
- Search inbox for status updates: `inbox_search scope=project:{project} msg_type=status_update`
- The workspace agent checks for completion on its next startup ritual

## Error Handling

- If the task workspace doesn't exist, `agent_spawn` raises `FileNotFoundError`
- If the CLI is unknown, it raises `ValueError`
- Spawned processes are fire-and-forget — crashes are visible in `agent.log`
- The workspace agent should check for stale/unresponsive tasks during triage
