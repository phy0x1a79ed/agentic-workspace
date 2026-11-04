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

**cleanup-pass**. Its result: `cleanup-pass/README.md`.

## Stack

- **cleanup-pass** (2026-11-03): when done, we need to schedule a pass to remove code to make source code cohesive and not have vestigial elements, and combining modules/funcitons that are duplicated

## Parked

## Closed

- **search** (closed 2026-11-03): `search/README.md`
- **scopes-awm** (closed 2026-11-03): `scopes-awm/README.md`
- **brief-inject** (closed 2026-11-03): `brief-inject/README.md`
- **plugin-core** (closed 2026-11-03): `plugin-core/README.md`
- **sessions-harness** (closed 2026-11-03): `sessions-harness/README.md`
- **queue-approver** (closed 2026-11-03): `queue-approver/README.md`
- **channels** (closed 2026-11-03): `channels/README.md`
- **journals** (closed 2026-11-03): `journals/README.md`
- **plugin-guards** (closed 2026-11-03): `plugin-guards/README.md`
- **search-filters** (closed 2026-11-03): `search-filters/README.md`
- **fleet-deploy** (closed 2026-11-03): `fleet-deploy/README.md`
- **trust-users** (closed 2026-11-03): `trust-users/README.md`
- **lane-open-items** (closed 2026-11-03): `lane-open-items/README.md`
