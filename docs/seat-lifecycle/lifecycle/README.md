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
