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

- **install-hold** (2026-11-06): install-loop 9fc4afc8 (mizar) and metasmith-install-loop 80f06e0c (altair) hold until Sat 2026-11-07 15:05 PT (Tony: 'Change to 305 PM Saturday, when my subscription refreshes')

## Stack

- **ops-board** (2026-11-06): https://nexus.tony-xy-liu.com/trilium/#root/iF0A3SWEGqUx/P4ALwPiCSs6N?ntxId=CZkmWI ops board needs updating
  - [x] Old AWM book: delete the old Ops password notes, carry Links into the new Ops, archive the old Swarm board? (answered 2026-11-06)
  - [x] Federation board: archive all 8 pre-redux cards, then check the door and intake end to end? (answered 2026-11-06)
  - [ ] Place the new Swarm board under the AWM book, next to the federation board?
- **end-state** (2026-11-06): there are a few sessions that look left over. Please use the DFS interview skill so that we can review the end state and if there are anything left open for us to clean up and do.
  - **session-cleanup** (2026-11-06): I also want to clean up the current list of sessions to prevent churn from restarts
  - **seat-lifecycle** (2026-11-06): need to update seats to retire after task is done with report sent back through DM. Seats should also be instructed to journal often -> we should also add tags for each user, and one that marks seats. otherwise, these journals are debreif journals. ... the task should be resumable from debrief journals, even if the sessions are gone.
  - **social-split** (2026-11-06): I think socials needs to be split into 2 access channels, maybe even 3. for write only -> notifications, read/write on channels (bots/seats), personal automations (old teams and slack browser stuff). browser is a utility and stays separate -> it can be part of core actually, it just doesnt keep any brwosers warm
  - **deploy-routine** (2026-11-06): we should change the deploy protocol to be deterministic with only agentic error handling, so that it becomes a (was it called routine?) like what was discussed during the initial redux spec -> do we have a journal for the redux spec so that future agents can refer to it? and can we edit it incrementally rather than full rewrite?
  - **queue-approvals-test** (2026-11-06): lets hold a full test of the new queue and approvals workflow in 1 sitting, prolly with another agent
  - **node-roster** (2026-11-06): can I get a board for this on awm? like an automatically updated roster with stats for each node? awm install has this as opt in, since not all installs will be for our swarm
  - **session-naming** (2026-11-06): we should maybe to naming by hand and omit agent renaming unless needed. and we can revise the naming scheme to include node. please push this onto tthe stack ... the job ids dont match up. -> yea, need to revise session naming.
  - **env-reload** (2026-11-06): (from nexus-signin) a changed config.toml [env] reaches plugins only after a gateway restart
  - **search-mcp** (2026-11-06): one thing we should review is that all nodes are using the new search service and MCP.
    - [x] Refresh shogun to b1e85c50 now? (answered 2026-11-06)

## Parked

## Workers

- **daae3825** (2026-11-06): took refresh-all
- **daae3825** (2026-11-06): took node-time
- **9fc4afc8** (2026-11-06): took install-loop
- **80f06e0c** (2026-11-06): took install-loop
- [x] tell daae3825 how shogun-singletons was solved (told 2026-11-06)
- **f6cd1c4b** (2026-11-06): took ops-board

## Closed

- **fir-ssh** (closed 2026-11-06): `fir-ssh/README.md`
- **refresh-all** (closed 2026-11-06): `end-state/refresh-all/README.md`
- **node-time** (closed 2026-11-06): `end-state/node-time/README.md`
- **nexus-signin** (closed 2026-11-06): `nexus-signin/README.md`
- **install-loop** (closed 2026-11-06): `install-loop/README.md`
- **shogun-singletons** (closed 2026-11-06): `end-state/shogun-singletons/README.md`
