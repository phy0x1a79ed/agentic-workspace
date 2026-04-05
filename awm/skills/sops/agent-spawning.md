---
name: agent-spawning
type: protocol
scope: workspace
tags: [agents, spawning, delegation, cli, scope]
requires: []
description: How to spawn scope agents, configure CLIs, and manage the fire-and-forget lifecycle
---

# Agent Spawning SOP

## Overview

The workspace agent can delegate work by spawning fire-and-forget agent subprocesses on scope workspaces. Each spawned agent runs in its own terminal session with access to the scope's `.awm/context.md`, inbox, and code.

## Spawning an Agent

Use the `agent_spawn` MCP tool:

```
agent_spawn project=<project> scope=<scope> [prompt="..."] [agent_cli=opencode|claude]
```

### What Happens

1. The CLI is resolved: explicit `agent_cli` param → config table `agent_cli` key → default `opencode`
2. The scope workspace is verified at `projects/{project}/{scope}/`
3. If a prompt is provided, it's sent as a `plan` message to `scope:{project}/{scope}` inbox
4. The agent process is spawned with `start_new_session=True` (detached)
5. Output is logged to `projects/{project}/{scope}/agent.log`

### CLI Options

| CLI | Command | Notes |
|-----|---------|-------|
| `opencode` | `opencode` in scope cwd | Interactive TUI — agent reads .awm/context.md on startup |
| `claude` | `claude --print -p <prompt>` in scope cwd | Non-interactive, prints result and exits |

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

1. Create the scope if it doesn't exist: `scope_create project=X scope=Y`
2. Optionally send context: the `prompt` parameter handles this automatically

### After Spawning

The spawned agent should:
1. Check inbox on startup: `inbox_search scope=scope:{project}/{scope}`
2. Read and acknowledge the plan: `inbox_read id=N`
3. Execute the work
4. Send a `reflection` message to `project:{project}` on completion
5. Log the session: `session_log`

### Monitoring

- Check `agent.log` in the scope workspace for raw output
- Search inbox for status updates: `inbox_search scope=project:{project} msg_type=status_update`
- The workspace agent checks for completion on its next startup ritual

## Error Handling

- If the scope workspace doesn't exist, `agent_spawn` raises `FileNotFoundError`
- If the CLI is unknown, it raises `ValueError`
- Spawned processes are fire-and-forget — crashes are visible in `agent.log`
- The workspace agent should check for stale/unresponsive scopes during triage
