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

**search**. Its result: `search/README.md`.

## Stack

- **search** (2026-11-03): can we just drop prior? it seems to not be worth it. can hops just be plain queries? what is the graph section? lets review the search architecture as one of the major systems
  - [ ] Drop the paper prior, and what orders the graph section without it?
  - [ ] Keep then on search query, or make hops a separate kb hop call?
  - [ ] Keep the graph section?
  - [ ] Drop the second PageRank that reorders journals, precedents and notes?
  - [ ] Cognee still serves graph and answer modes in kb, against R136 item 3: retire it now?
- **scopes-awm** (2026-11-03): I dont get this, why are ignored files blocking delete? what is checking them? dvc is versioend with git via pins and should work lke any other file. we should talk about this. we should also review the .awm folder
  - [ ] Survey done: see /home/tony/.claude/jobs/8162a704/tmp/scopes-survey.md; copy into scopes-awm/README.md when current
- **plugin-core** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **sessions-harness** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **queue-approver** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **channels** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **fleet-deploy** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **trust-users** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned

## Parked

## Closed
