# Seat design

## Purpose & Contents

This doc holds the current design of seats: the chats Opal answers on Slack and Discord, and the agents that answer them. It is the single source for anyone who builds seats. It states the design as decided. The topic folders beside it hold the discussion, Tony's words and the surveys, including decisions that later ones replaced. Decision journals 87836740 through 6ec07350 (project awm, scope seat-pause, tag `decision`) record each decision.

## Why

On 2026-11-06 a seat restart started five per-person agents with a brief that said "answer anything unanswered". Two of them answered old messages without Tony. One message that reached a dead agent was lost. Seats on altair were paused, and this design replaced the old model.

## Seats

1. A seat is one chat: a DM, a group DM or a channel. A seat never spans chats.
2. Every message in a seated chat goes to that seat, @mentions included. A message in a chat with no seat goes nowhere, on purpose.
3. A seat may forward a message to another seat, such as a channel seat to a person's DM seat. The receiving agent may read the source chat's history for context.
4. A DM seat exists only for a person who wrote to Opal. Opal never messages a person first: `send` refuses a DM to a person who never wrote there (redux `users.md` R126 item 1).
5. Seats are declared in config: every Slack DM by rule, plus explicit seats. On 2026-11-06 the one channel seat is Discord Hallam Hive #general, at the cooperative level: it is invite-only, lab-only, and the lab's agentic group chat. Discord #bots, Slack #arbutus and #bioinformatics have none. #arbutus carries notifications only.
6. A DM from a new person creates their DM seat, notifies Tony with `request_human`, and starts its agent.
7. A sender from outside the lab gets no agent. The message is held and Tony is asked.

## Where agents run

1. Every seat agent runs on altair, except Tony's Discord DM seat (mizar).
2. Tony has two seats: his Slack DM on altair and his Discord DM on mizar. Both are trusted. Their briefs carry redux `sessions.md` R67 and R68.
3. Agents run on Opus at high effort.
4. Agents start through `reflection start_other` on the seat's node, which claims a warm spare. The first action comes about 7 s after the start.

## Seat states

A seat is `empty`, `starting`, `occupied` or `retiring`. social holds the state and a queue of inbound messages for each seat.

1. **empty**, message arrives: social reacts 👀 to it, queues it, and starts an agent with the queue. The seat is `starting`.
2. **starting**: later messages queue. When the agent mounts the seat, social delivers the queue and the seat is `occupied`. A start that never mounts is retried, then reported (retry limit).
3. **occupied**, message arrives: social pushes it to the agent. The agent works, reports in the chat, journals, compacts and waits.
4. The agent marks its seat done with a social verb whose name says so, such as `mark_seat_done`. A new message clears the mark and restarts the clock.
5. **occupied**, done for 4 hours with no new message: social tells the agent to write a short handoff journal and retire. This is not a full debrief. The seat is `retiring`.
6. **retiring**, message arrives: social answers in the chat, "agent is changing shift, will respond within 5 mins", and queues the message.
7. **retiring**, agent gone: the seat is `empty`. A queued message starts the next agent as in step 1.
8. **retiring** past a 4-minute deadline: social does not kill the old agent. It starts the new agent with the queue and a note naming the previous agent's job and transcript. The note tells it to look for the handoff journal, and to read that transcript if the journal is not there yet.
9. A push that fails, or finds its agent gone, never drops the message. The message goes into the queue, and the seat goes to `empty` handling in step 1.
10. After a social restart, the messages its resync finds after the last sync point go through seats as new messages. Nothing older does.
11. No agent runs idle past the 4-hour reclaim. A backlog, a restart or a handoff journal never starts an agent. Only a new message does.

## One component

The old keeper (`keeper.py`) merges into seats. One seats component in social owns every declared seat: state, queue, routing, cold start, the 4-hour reclaim, the retry limit and down notices. The word "keeper" goes. A hand `mount` still works, for temporary seats. No routine and no routine event trigger take part.

## Homes and journals

1. Seat work lives in `agentic-commons`, an awm project apart from awm and metasmith (redux `users.md` R128). Its checkout `projects/agentic-commons/main/` holds the SOP, the briefs and `scripts/new-seat.sh`.
2. Each seat has a folder beside the checkout, `projects/agentic-commons/seats/<slug>/`: a local git repo with DVC and no remote. It is registered as the awm scope `seats/<slug>`, with the folder as its worktree. Tony, 2026-11-07: "Beside the checkout".
3. `scripts/new-seat.sh <slug>` makes the folder and registers the scope. Run it on the seat's node before the seat's first start. Without it, `start_other` would make a full worktree of the repo, and the journal refuses posts to the scope.
4. social starts the agent with a plain `start_other` on project `agentic-commons`, scope `seats/<slug>`. The agent claims its seat scope, so one agent holds a seat at a time. It reads its scope's journals for context when it needs them. A task stays resumable after its session is gone.
5. Every seat journal carries the tags `journal`, `seat` and `user:<person-slug>`. A person's continuity across seats comes from the user tag.
6. An agent journals after each result or decision it sends, and once at retire.

## The pushed line

1. Each line social types into an agent's prompt opens with `[<server>/<chat>/<sender>, <level>]`, then `↳<thread id>` for a thread reply, then the text. `<server>` is the platform for a DM, and the workspace or guild name for a channel.
   - `[slack/DM/Steven Chen, cooperative] is it running?`
   - `[slack/DM/Steven Chen, cooperative] ↳1793825822.808289 just using a strict ...`
   - `[Hallam Hive/#general/beefboff, untrusted] ...`
2. No `from`, no channel id and no message id. `send` and `react` from a seat's agent default to the chat, thread and message of its latest line. The agent names a thread by its id.
3. Seat session names read `seat:<app>/<server>/<chat>`, with one slug per person, the same as the scope and the user tag.

## When an agent speaks

1. An agent answers the message that started it and reports on that task only. It raises an older thread or promise only when the person asks about it. This narrows precedence d6654d87.
2. A channel seat is for collaborative work. "@Opal help Dana" means help Dana in that channel.
3. A channel seat decides by judgement whether a message belongs to a person's DM seat, and forwards it if so.
4. Lab seats are at the cooperative level (redux `channels.md` R125): no cards, at most 5 subagents, no Tony personal information.

## Briefs

1. A general seat SOP covers every platform: lifecycle, journaling, when to speak, trust levels, first contact, forwarding and reaching Tony.
2. One brief per server covers what is local to it. The Hallam lab Slack on altair gets clusters, tools, the tether and lab voice. Each Discord server gets its own.
3. social composes the SOP, the server brief and the seat facts into the starting brief: chat, trust level (stated once here, not on each line), scope, queued messages, and the previous-agent note.
4. The SOP and the server briefs live in `agentic-commons/docs`. The `opal-seat-holder` skill retires.

## Operator notices and commands

1. Operator notices (seat down and recovered, cch keep, ssh lock alerts) stay bot posts in Tony's Discord DM. They start nothing. When Tony writes, his seat's agent reads the DM's recent history, notices included.
2. The Discord `/new-agent` command goes. The board's new-agent button (trilium-design) takes over.
3. Remote Control on and off for seats belongs to the fleet dashboard (trilium-design/interview).

## State on 2026-11-07

- Seats on altair are paused until the Rust social port (plugin-redux) lands. The pause and how to undo it are in journal 214c7abd. The backups are at `~/.local/share/awm/seat-pause-backup` on altair.
- No temporary seat runs. `agentic-commons` exists on altair with 15 seeded seats. The cutover steps are in `handoff/README.md`.
