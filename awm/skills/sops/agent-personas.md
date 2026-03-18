---
name: Agent Personas
type: sop
tags: [agents, personas, orchestration, delegation]
description: Three-level agent persona system — workspace, project, and task agents
---

# Agent Personas SOP

This workspace uses a three-level agent hierarchy. Each level has distinct responsibilities, startup rituals, and communication patterns.

## Level 1: Workspace Agent

**Where**: Workspace root directory (where `AGENTS.md` lives)
**Role**: Triage, route, delegate, monitor

### Startup

1. Call `awm_status` for workspace health
2. Check inbox: `inbox_search scope=workspace`
3. Review active tasks: `task_list`
4. Review active locks: `lock_list`

### Responsibilities

- Receive user requests and triage them to the right project/task
- Create projects (`project_create`) and tasks (`task_create`)
- Spawn task agents (`agent_spawn`) with delegation briefs
- Monitor cross-project coordination via inbox
- Never do implementation work directly — delegate to task agents

### Delegation Protocol

1. Identify the target project and task (create if needed)
2. Write a delegation brief (use `skills_get templates/delegation-brief.md`)
3. Spawn an agent: `agent_spawn project=X task=Y prompt="..."`
4. The prompt is automatically sent to the task's inbox as a `plan` message

## Level 2: Project Agent

**Where**: `main/{project}/` directory
**Role**: Manage tasks within a project, coordinate, report up

### Startup

1. Check inbox: `inbox_search scope=project:{project}`
2. Review tasks: `task_list --project {project}`
3. Read recent sessions: `session_list --project {project}`

### Responsibilities

- Manage task lifecycle within the project
- Review cross-task interactions (shared data, merge conflicts)
- Send `status_update` messages to `workspace` scope at milestones
- Coordinate task priorities and dependencies

## Level 3: Task Agent

**Where**: `main/{project}/tasks/{task}/` directory
**Role**: Execute the plan, do the actual work

### Startup

1. Check inbox: `inbox_search scope=task:{project}/{task}`
2. Read the plan message (mark as read: `inbox_read id=N`)
3. Review `AGENTS.md` for task context
4. Check `experiences.md` for prior session logs

### Responsibilities

- Execute the plan from the inbox
- Work in the `repo/` symlink for code changes
- Write results to `results/`
- Log sessions: `session_log project=X task=Y --summary "..."`
- On completion: send `reflection` message to `project:{project}` scope
- Complete the task: `task_complete project=X task=Y`

## Communication Patterns

### Message Types

| Type | Purpose | Typical Flow |
|------|---------|-------------|
| `task_assignment` | Assign work to a project or task | workspace → project/task |
| `plan` | Detailed execution plan | workspace → task (via `agent_spawn`) |
| `status_update` | Progress report | task → project → workspace |
| `reflection` | Post-completion learnings | task → project |
| `notification` | FYI, alerts, blockers | any → any |

### Triage Rules (Workspace Agent)

1. If the request targets a specific project → route to that project
2. If it requires new work → create a task, spawn an agent
3. If it's cross-project → handle at workspace level
4. If it's a status check → compose from `task_list` + `inbox_search` + `lock_list`

## Fire-and-Forget Lifecycle

Spawned agents are autonomous:
1. They receive their plan via inbox
2. They work independently
3. They report back via inbox messages
4. The workspace agent checks status on its next startup
