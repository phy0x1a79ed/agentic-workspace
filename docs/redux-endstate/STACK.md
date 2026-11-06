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

**refresh-all**, under **end-state**. Its result: `end-state/refresh-all/README.md`.

## Stack

- **end-state** (2026-11-06): there are a few sessions that look left over. Please use the DFS interview skill so that we can review the end state and if there are anything left open for us to clean up and do.
  - **refresh-all** (2026-11-06): Is Shogun the only one? We should aim to refresh all.
    - [ ] Add shogun to the deploy fleet with its own daemon, and make capella start awm at boot?
  - **node-time** (2026-11-06): Also, can you check the times across nodes? The time should be 10 a.m.
    - [ ] Set every node to America/Vancouver through the cloud Ansible baseline, with current tzdata?
  - **search-mcp** (2026-11-06): one thing we should review is that all nodes are using the new search service and MCP.
    - [x] Refresh shogun to b1e85c50 now? (answered 2026-11-06)

## Parked

## Closed

- **fir-ssh** (closed 2026-11-06): `fir-ssh/README.md`
