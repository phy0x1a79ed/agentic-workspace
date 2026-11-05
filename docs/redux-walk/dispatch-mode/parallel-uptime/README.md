# parallel-uptime

## Purpose & Contents

This topic holds the part of dispatch mode that keeps Tony and Swarm triage both working at once. It is part of dispatch mode, not a later item.

## Tony's words

> I'd like to be able to work in parallel. if all discussion goes through you, then I sit idle while you work and vice versa. our first topic should be a protocol taht maximizes both of our up times

> I think the parallel up-time discussion actually ties into this, not as a next item

> the ready queue works, but is problematic since as new things enter your context, the queue and questions gets lost in the transcript

> I guess this is what the board is for... I can still type here, we just need a presentation panel or bullitin board to pin local, currently pending tasks

> 4 makes sense

## Design so far

1. Swarm triage keeps its turns short. It hands longer work to a worker or a subagent.
2. Several independent topics stay open at once. Each pending question for Tony stands alone and carries a recommendation.
3. The pending items live in a file in this folder, not in the transcript. A pane beside the chat shows that file and refreshes when the file changes. Tony answers by typing in the chat.
4. A worker whose task needs design input from Tony talks with him directly, as rt-container does. It sends its decisions to Swarm triage.

## Open

- Build the board file and the pane first.
