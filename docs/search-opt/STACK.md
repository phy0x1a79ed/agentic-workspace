# search-opt: stack

## Purpose

Settle the specification of disk-based search for agentic memory before any design: what it serves, its sources, its memory and latency bounds, and its matching. The search signature is already decided and is an input, not a topic.

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

**sort**, under **spec**. Its result: `spec/sort/README.md`.

## Stack

- **spec** (2026-11-04): Basic rough spec is disk based search over text, mainly journals, for agentic memory.
  - **sort** (2026-11-04): Order however necessary and query will reorder by relavence whatever that means -> maybe need to add a sort by schema in querry
    - [ ] Where in the query does a source's sort go?
  - **memory** (2026-11-04): Want disk because keeping a large corpus in ram is not feasible with essentially unlimited size. But something like 100mb is acceptable.
  - **latency** (2026-11-04): It needs to be fast because agents will constantly call it so something like 1s is prolly max, but flexible. Simple queries like last journal or journals about x where x is specific/obvious should return fast like 0.1s.
  - **matching** (2026-11-04): Search requires both exact and semantic matches.

## Parked

- Earlier inputs from 367a5d0d: "Im Suggesting to use pagerank as an optimization for disk based search. So that it doesnt have to scan the whole corpus." It enters the stack at design time.
- "We can then use this to hold each of the sources separately." It enters with **sources**.
- Surveys of disk-based methods: `surveys/keyword.md` and `surveys/semantic.md`. They enter at design time.
- Implementation questions set aside on 2026-11-05: which node and cold or warm for the gate, and whether the journal export contract may change.

## Closed

- **sources** (closed 2026-11-04): `spec/sources/README.md`
