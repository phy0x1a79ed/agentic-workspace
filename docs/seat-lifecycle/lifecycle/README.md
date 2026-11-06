# lifecycle

## Purpose & Contents

This file holds the decided lifecycle of a Slack seat agent on the `slack-bot` account: what runs all the time, what starts a per-client agent, and when that agent ends.

## Tony's words

> need to update seats to retire after task is done with report sent back through DM. ... even for seats of lab messages and DMs, they should retire and not just sit there.

On 2026-11-06, relayed by redux-endstate/review, about his 18:37Z message to slack-main:

> this was my original message, I meant to restart after a the main seat got a message from the user's DM. I want to stop all per client seats, then swtich the reciever back to slack main. slack main then restarts and hands back the seats only once AFTER it gets a new DM message from that client

## Design

Tony, 2026-11-06 19:28Z:

> can we register triggers? the design should be: slack DM -> if agent exists -> agent seat/ else triggers new seat routine -> starts agent with brief and instructing to look for previous journals for context if needed.

1. A Slack DM arrives on a `slack-bot` seat.
2. If a live agent holds the seat, social pushes the message to that agent.
3. Otherwise the message triggers the new-seat routine. The routine is deterministic code, not a Claude session.
4. The routine starts an agent for that client with the seat brief and the message. The brief tells the agent to read the client's previous journals for context when it needs them.
5. The agent does the task. It sends its report in the client's DM, journals and retires.
6. No per-client agent runs idle. A backlog, a restart or a handoff journal never starts an agent. Only a new message does.

This replaces the long-lived slack-main receiver from Tony's 18:37Z intent ("swtich the reciever back to slack main"). The routine takes slack-main's routing role.

## Incident this design prevents

On 2026-11-06 slack-main read "restart seats on DM" as "start every per-client agent now". Its brief told each agent to "Answer anything unanswered". Steven's and Kalen's agents answered backlog messages, and Kalen's agent also sent an unrequested follow-up from 10-30.

## What exists today (survey, 2026-11-06)

- When a holder's job dies, the next push to it comes back "gone". That message is thrown away, and the seat is released (`awm/social/push.py:73-81`). A prune every 60 s also releases dead holders (`seats.py:100-125`).
- A released seat pushes to nobody (`hub_adapter.py:148-152`). Only `social history` keeps its messages.
- A per-client agent hands back with `social unmount`. slack-main took the seat again only through the hourly sweep cron (`SKILL.md:27-28, 65`). Tony dropped that sweep on 2026-11-02, so nothing takes a released seat back now.
- A kept seat with `channels = "all"` remounts every chat its agent does not hold, every 60 s (`keeper.py:992-1001`). It also takes a seat back from a running per-client agent, and it refuses other sessions' mounts of its seats (`hub_adapter.py:574-580`). It cannot serve as slack-main in this design as it stands.

## The routine tool (survey, 2026-11-06)

Tony: "we can actually make this a deterministic routine, check that the awm tool exists".

- `routine` exists on altair (`awm/plugins/routine`). A routine is a TOML definition in a project repo (`routines/full-tests.toml` is the model): shell steps, a trigger, and an optional agent.
- Triggers are a systemd timer or a call (`routine trigger`). No event trigger exists. Social would have to call `routine trigger` itself.
- A `condition` step that exits 0 dispatches one agent. Its scope is fixed by the definition (`runner.py:157-168`), so it cannot pick the client's scope. A plain step script can call `awm reflection start_other` with the client's scope and message instead.
- A run is refused while the routine has a live run (`trigger` verb).

## Event triggers (survey, 2026-11-06)

- Social already emits every inbound message on its `message` topic, before it pushes to the holder (`awm/social/hub_adapter.py:134-152`).
- A service can subscribe to a topic with `gatewayclient.subscribe(service, topic)`. events, 2fa, search, ssh and operations already do.
- The routine plugin registers only timer and call triggers. An event trigger is a new routine feature: subscribe to a topic, filter the payload, and hand the payload to the steps.

## Decision: event triggers

Tony, 2026-11-06, to "should I add event triggers to the routine plugin?": "yes".

- The routine plugin gains an event trigger: a definition registers `[trigger] event = "social/message"` with a filter (slack-bot DMs).
- The plugin subscribes to the topic and hands each message to the routine's steps.
- The new-seat routine's script checks for a live holder on the seat. When there is none, it starts the client's agent with the seat brief and the message.
