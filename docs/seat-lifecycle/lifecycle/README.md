# lifecycle

## Purpose & Contents

This file holds the decided lifecycle of a Slack seat agent on the `slack-bot` account: what runs all the time, what starts a per-client agent, and when that agent ends.

## Tony's words

> need to update seats to retire after task is done with report sent back through DM. ... even for seats of lab messages and DMs, they should retire and not just sit there.

On 2026-11-06, relayed by redux-endstate/review, about his 18:37Z message to slack-main:

> this was my original message, I meant to restart after a the main seat got a message from the user's DM. I want to stop all per client seats, then swtich the reciever back to slack main. slack main then restarts and hands back the seats only once AFTER it gets a new DM message from that client

## Design

1. slack-main is the one long-lived receiver. It holds every `slack-bot` seat that no per-client agent holds, DMs and lab channels alike.
2. No per-client agent runs idle.
3. slack-main starts a client's agent only when a new message from that client arrives. It hands that message to the new agent. A backlog sweep, a restart or a handoff journal never starts an agent.
4. The client's agent does the task. It sends its report in the client's DM, journals and retires.
5. When the agent retires, the client's seat goes back to slack-main.

## Incident this design prevents

On 2026-11-06 slack-main read "restart seats on DM" as "start every per-client agent now". Its brief told each agent to "Answer anything unanswered". Steven's and Kalen's agents answered backlog messages, and Kalen's agent also sent an unrequested follow-up from 10-30.
