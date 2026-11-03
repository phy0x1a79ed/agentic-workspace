# redux-review: stack

## Purpose

Review each major awm system's architecture after the first build night, so the build and the redux design (docs/redux/) still agree. Record each settled change as a requirement in its docs/redux area doc.

Procedure, from dfs-interview § Work the stack:

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
8. Write the result as the current design.
9. Tick each answered question with `stack.py <folder> answer`.
10. Run `stack.py <folder> close` when the topic's question queue is empty.
11. Commit.
12. Run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Current topic

**plugin-core**. Its result: `plugin-core/README.md`.

## Stack

- **plugin-core** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
  - [x] Which plugins are core and stay in the awm repo (R115 item 4), and which move into projects of their own (routines.md R33)? (answered 2026-11-03)
  - [x] Does resolving tool UUIDs at gateway start, with a restart on rename, meet R117 item 3, which compiles names in at install? (answered 2026-11-03)
- **sessions-harness** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **queue-approver** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **channels** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **fleet-deploy** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
  - [ ] Which repo is canonical for awm: GitHub (install.md R114, R118) or mizar (routines.md R31)? Today origin is altair.
- **trust-users** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned

## Parked

## Closed

- **search** (closed 2026-11-03): `search/README.md`
- **scopes-awm** (closed 2026-11-03): `scopes-awm/README.md`
- **brief-inject** (closed 2026-11-03): `brief-inject/README.md`
