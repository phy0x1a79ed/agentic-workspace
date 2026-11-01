# Parallel work

## Purpose & Contents

This doc holds the parallel work skill. The skill tells an agent how to plan a large task in waves across top-level sessions, when each session claims and releases a scope, and how the work is gathered and deployed. Thread 1p produces it.

Related rules live in other docs:

- `dispatch.md` R50 sets when an agent may start a top-level session.
- `sessions.md` R51 and R52 hold the wait and the paths for work that spans sessions. R56 to R58 hold names and task ids.
- `scopes.md` R2 and R42 hold how a session enters and claims a scope.

## Requirements

- **R59.** A parallel work skill guides a large task. It tells the agent how to plan in waves split across agents, when to claim and release a scope, and how to gather and deploy the work. Tony: "I think we need a parallel work skill for large tasks, that instructs to plan in waves split across agents, when to scope claim and unclaim, gathering and deploying." (2026-11-01).
  - The skill serves `sessions.md` R52 path 2. `dispatch.md` R50 step 1 names it.

## Gap from today

Surveyed read only on 2026-11-01.

- **R59 missing.** No skill plans work in waves or starts top-level sessions. The nearest skills under `~/.claude/skills` are these:
  1. `autopilot` drives an approved plan through its phases. It uses subagents for review. It starts no top-level session.
  2. `scatter-gather` merges a set of `feat/<p>` branches into a hub branch, and the hub back out to them, through `scope gather` and `scope scatter`. The hub map lives in `PROTOCOLS.md` § Hubs and peripherals. It plans nothing.
  3. `retire` debriefs, reports to the session that started it and stops the job.
- **No claim and no release.** `reflection cd` types `/cd` into the session. It trusts an awm worktree first (`reflection/control.py`, `cd`). It records no claim and checks no other session (`scopes.md` gap, R3 and R42). No verb releases a scope. A session leaves a scope only when it moves or ends.
- The wait and the deploy are missing too. See the `sessions.md` gap for R51 and the `routines.md` gap for R26.
