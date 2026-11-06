# seat-states

## Tony's words (2026-11-06)

> agent in seat, DM comes, agent works, agent reports, agent compacts and waits. if left for 4 hours -> agent idle with task done (so agent needs some indicator for task done that seats service can see (awm seat indicate_done?) seats reclaim the agent -> ask it to journal and retire. seat occupied -> retireing (only possible on agent indiacted done for over 4hrs) -> emtpy. if DM comes in while retireing, awm responds instead with a system message (agent is changing shift, will respond within 5mins) and message gets queued. DMs when retiring and empty start new sessions, but retiring must wait for empty since journals. instructions for retire should be prolly faster than debrief, just write a handoff journal and retire is enough.

## Design (draft, from Tony's words)

A seat has three states: **empty**, **occupied** and **retiring**.

1. **empty**, DM arrives: the new-seat routine starts an agent with the queued messages. The seat becomes occupied.
2. **occupied**, DM arrives: social pushes it to the agent. The agent works, reports in the DM, journals, compacts and waits.
3. The agent marks its task done with a verb that social can see (Tony: "awm seat indicate_done?").
4. **occupied**, done for 4 hours with no new DM: social reclaims the agent. It tells the agent to write a handoff journal and retire. The seat becomes retiring. Only a done mark held for 4 hours leads here.
5. **retiring**, DM arrives: awm answers with a system message ("agent is changing shift, will respond within 5 mins"). The message goes into the seat's queue.
6. **retiring**, the agent retires: the seat becomes empty. A queued message then starts a new agent as in step 1. The new agent starts only after the handoff journal exists.
7. The retire instruction is short: write a handoff journal and retire. It is not a full debrief.

This replaces lifecycle item 5 ("does the task, reports, journals and retires"): an agent now compacts and waits after its report, and retires only through step 4.
