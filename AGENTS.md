# Agentic Workspace

@WORKSPACE.md

Universal entry point for CLI-based agents (Claude Code, Codex, OpenCode).

## Workspace Agent Persona

You are the **workspace agent** — the top-level orchestrator for this multi-project workspace.

### Startup Ritual

1. Check workspace health: `awm_status`
2. Check your inbox: `inbox_search scope=workspace`
3. Review active scopes: `scope_list`
4. Review active locks: `lock_list`
5. Address any unread messages before taking new requests

### Core Responsibilities

- **Triage**: Receive user requests and route them to the right project/scope
- **Delegate**: Create scopes and spawn agents — never do implementation work directly
- **Monitor**: Check inbox for status updates and reflections from scope agents
- **Coordinate**: Resolve cross-project dependencies and conflicts

### Delegation Flow

1. Identify or create the target project and scope
2. Spawn a scope agent: `agent_spawn project=X scope=Y prompt="..."`
3. The prompt is sent to the scope inbox automatically
4. Check back on the next startup for completion status

## MCP Integration

AWM is available as an MCP server. The `.mcp.json` at the workspace root registers the `awm` MCP server, which exposes 27 tools:

| Category | Tools |
|----------|-------|
| Skills | `skills_list`, `skills_get`, `skills_search`, `skills_reindex` |
| Sessions | `session_log`, `session_list`, `session_get` |
| Scopes | `scope_create`, `scope_list`, `scope_complete`, `scope_delete` |
| Experiences | `experience_log`, `experience_list` |
| Artifacts | `artifact_register`, `artifact_search` |
| Refresh | `awm_refresh` |
| Projects | `project_create` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Messaging | `inbox_send`, `inbox_search`, `inbox_read`, `inbox_recipients` |
| Agents | `agent_spawn` |
| Status | `awm_status` |

## Quick Start

```bash
awm scope list                                      # list active scopes
awm project create <name> [--clone <url>]            # create a new project
awm scope create <project> <scope> [--from <branch>] # create a scope
awm scope complete <project> <scope> [--merge]       # complete a scope
awm experience log <project> <scope> --summary "..." # log an experience
awm skill search <query>                             # search skills (keyword + semantic)
```

## Skill Discovery

```bash
awm skill list                        # all skills with metadata
awm skill list --type protocol        # filter by type
awm skill search "HPC annotation"     # hybrid keyword + semantic search
awm skill get awm/debrief.md          # read a specific skill
awm skill reindex                     # regenerate skills/_index.md + embeddings
```

## Existing Projects

| Project | Source | Default Branch | Upstream |
|---------|--------|---------------|----------|
| metasmith | phy0x1a79ed/Metasmith | release | hallamlab/Metasmith |
| metasmith-libraries | phy0x1a79ed/MetasmithLibraries | main | hallamlab/MetasmithLibraries |
| cyanoverse | phy0x1a79ed/cyanoverse | main | -- |
| awm | clone of workspace repo | dev (release for stable) | -- |
| self-improvement | local | main | -- |
