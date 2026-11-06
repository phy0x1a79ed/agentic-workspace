# redux-endstate: stack

## Purpose

Review the end state of the redux rollout across all awm nodes, find what is left open (leftover sessions, cards, follow-ups) and clean it up; first fix Slack seats on altair failing to ssh into fir.

Procedure, from dfs-interview § Work the stack:

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Match each answer to the message it replies to. Ask when a reply could answer more than one message.
8. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
9. Write the result as the current design.
10. Tick each answered question with `stack.py <folder> answer`.
11. Run `stack.py <folder> close` when the topic's question queue is empty.
12. Commit.
13. Compact only when the closed topic ran long, with many exchanges or large reads. After a short topic, present the next topic in the same turn.
14. To compact, run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Current topic

**ops-board**. Its result: `ops-board/README.md`.

## Pending

- **shogun-callers** (2026-11-06): Which of alcor's singletons may shogun call? Recommend operations only (approvals R100, board).
- **cloud-delay** (2026-11-06): fleet-endstate holds its node changes until 15:05 PT Fri 2026-11-06 (Tony: 'delay them to start this friday at 305 PM')

## Stack

- **install-loop** (2026-11-06): I want to start a session locally to iteratively dev on awm with cloud agent install tests so that the process is seamless and as close to 1 command as possible, including all core plugins (we can also discuss what that means, starting from the current definition, in that session) -> it would be good to do this for metasmith on altair as well
- **ops-board** (2026-11-06): https://nexus.tony-xy-liu.com/trilium/#root/iF0A3SWEGqUx/P4ALwPiCSs6N?ntxId=CZkmWI ops board needs updating
  - [ ] Old AWM book: delete the old Ops password notes, carry Links into the new Ops, archive the old Swarm board?
  - [x] Federation board: archive all 8 pre-redux cards, then check the door and intake end to end? (answered 2026-11-06)
  - [ ] Place the new Swarm board under the AWM book, next to the federation board?
- **end-state** (2026-11-06): there are a few sessions that look left over. Please use the DFS interview skill so that we can review the end state and if there are anything left open for us to clean up and do.
  - **queue-approvals-test** (2026-11-06): lets hold a full test of the new queue and approvals workflow in 1 sitting, prolly with another agent
  - **node-roster** (2026-11-06): can I get a board for this on awm? like an automatically updated roster with stats for each node? awm install has this as opt in, since not all installs will be for our swarm
  - **session-naming** (2026-11-06): we should maybe to naming by hand and omit agent renaming unless needed. and we can revise the naming scheme to include node. please push this onto tthe stack ... the job ids dont match up. -> yea, need to revise session naming.
  - **shogun-singletons** (2026-11-06): from daae3825: After the render, shogun reaches neither alcor's operations and browser nor any other singleton. ... Should I do (b), and for which tools? Without an answer I do only (a) and report shogun's singleton access as open.
  - **env-reload** (2026-11-06): (from nexus-signin) a changed config.toml [env] reaches plugins only after a gateway restart
  - **search-mcp** (2026-11-06): one thing we should review is that all nodes are using the new search service and MCP.
    - [x] Refresh shogun to b1e85c50 now? (answered 2026-11-06)

## Parked

## Workers

- **daae3825** (2026-11-06): took refresh-all
- **daae3825** (2026-11-06): took node-time

## Closed

- **fir-ssh** (closed 2026-11-06): `fir-ssh/README.md`
- **refresh-all** (closed 2026-11-06): `end-state/refresh-all/README.md`
- **node-time** (closed 2026-11-06): `end-state/node-time/README.md`
- **nexus-signin** (closed 2026-11-06): `nexus-signin/README.md`
