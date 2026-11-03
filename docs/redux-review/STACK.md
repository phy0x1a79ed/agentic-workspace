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

**queue-approver**. Its result: `queue-approver/README.md`.

## Stack

- **queue-approver** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
  - [x] alcor reaches deneb for the writer and the panel tunnel, against R87 item 3 'alcor reaches mizar only'. Treat both as the singleton access layer (R89) and send the writer through the trilium tool (R105)? (answered 2026-11-03)
  - [ ] Ryan's Arbutus approvals on Slack (users.md R123) have no home after the cutover drops request_human kind approval. Keep the Slack path as is, beside the queue?
  - [ ] The lane drops the urgent Discord DM, and keeps a Needs you card there after Tony answers. Restore the move back to Working on Tony's prompt (R61 item 2), and drop the DM?
  - [ ] Accept the lane's values: 12 h window cap, burst 1/2/4, 60 s pending expiry (R65 item 4), denied as a history outcome (R97 item 1), spawn body hash check?
  - [x] Tony: "what is the trillium tool?  what is the board tool? I sense some redundancies that we should clear up here". Clear the overlap of trilium, board, reporting/operations and the lane's queue, queue-store and approver tools. (answered 2026-11-03)
- **channels** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
- **fleet-deploy** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
  - [ ] Which repo is canonical for awm: GitHub (install.md R114, R118) or mizar (routines.md R31)? Today origin is altair.
  - [ ] fleet.md R109: the harness lane proposes item 7, an access-token login and a caller-token door for isolated sessions. Accept it?
- **trust-users** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned

## Parked

## Closed

- **search** (closed 2026-11-03): `search/README.md`
- **scopes-awm** (closed 2026-11-03): `scopes-awm/README.md`
- **brief-inject** (closed 2026-11-03): `brief-inject/README.md`
- **plugin-core** (closed 2026-11-03): `plugin-core/README.md`
- **sessions-harness** (closed 2026-11-03): `sessions-harness/README.md`
