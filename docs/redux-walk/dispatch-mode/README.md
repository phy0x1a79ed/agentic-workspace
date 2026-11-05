# dispatch-mode

## Purpose & Contents

This topic holds the dispatch mode that the redux walk runs in, and where the skill records it.

## Tony's words

> as tasks are decided and worked out, please dispatch top level agents to work on them
> their issues can be pushed back to the stack
> we will notify them once their issues are solved and how, rather than when we decide how to solve.
> we can add this mode to dfs-interview skill

## Design

The dfs-interview skill carries the mode in § Dispatch workers. `stack.py` records it in an optional Workers section of `STACK.md`:

1. `dispatch <topic> <worker>` records that a worker took a decided topic.
2. `push --from <worker>` marks a topic as a worker's issue.
3. `close` on such a topic records a notice owed to the worker.
4. `notify <topic>` ticks the notice once the worker is told how its issue was solved.
5. `show` lists the owed notices first.

Swarm triage is the only session that talks with Tony. rt-container talks with Tony directly because Tony asked for it.
