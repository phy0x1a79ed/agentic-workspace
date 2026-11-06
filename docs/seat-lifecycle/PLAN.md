# Seat lifecycle build plan

## Context

On 2026-11-06 stopped Slack seats came back and two of them answered old messages without Tony. Seats on altair are paused. The interview in this folder settled how seats work from now on (decision journals 87836740 through 5660abb4). This plan builds that design on `feat/seat-pause`. It does not deploy: Tony deploys once the deploy protocol update lands.

## What you said

> just finish feature and report back when done. dont deploy if you planend to. deploy protocol is being updated as well

> slack DM -> if agent exists -> agent seat/ else triggers new seat routine -> starts agent with brief and instructing to look for previous journals for context if needed.

> agent in seat, DM comes, agent works, agent reports, agent compacts and waits. if left for 4 hours -> agent idle with task done ... seats reclaim the agent -> ask it to journal and retire.

> maybe have name and channel always, otherwise may get lost. -> maybe ew need to actually do something like [slack/DM/steven chen, cooperative], or [Hallam hive/#general/some name here]

## Issues

- I1. A seat's agent runs until someone stops it, and a restart brief can make it answer old messages.
- I2. A message that reaches a dead or unreachable agent is dropped, and nothing starts a new one.
- I3. Social has no record of whether a seat's task is done, so nothing can reclaim an idle agent.
- I4. The routine plugin runs only on a timer or a call, so it cannot react to a message.
- I5. A seat is one chat, so one person's DM and channel mentions split across agents.
- I6. The pushed line carries ids and no chat name, and the agent must copy ids back to answer.
- I7. Nothing in code stops Opal from messaging a person who never wrote (redux R126 item 1).
- I8. Seat rules live in four places, none of them current, and none general enough for Discord.
- I9. Seat journals carry no seat or person tag, and seats journal only at debrief.

## High-level goals

- G1. A seat's agent exists only while there is work, and comes back from journals when work returns.
- G2. No message to a seat is ever lost.
- G3. A deterministic mechanism, not a Claude session, decides when an agent starts.
- G4. An agent reads at a glance where each message came from and who sent it.
- G5. Opal never speaks first, and an agent speaks only about the task it was started for.
- G6. One seat SOP serves every platform, with a short brief per server.

## Acceptance criteria

- A DM to a seat with no live agent emits `social/seat_needed` once, and the seat is `starting` until an agent mounts it. Later DMs queue and reach the agent on mount.
- `social mark_seat_done` marks the caller's seat done. A new DM clears the mark. After 4 hours done, social sends the retire instruction and the seat is `retiring`.
- A DM to a `retiring` seat gets the shift-change reply and is queued. When the agent is gone, or after a 4-minute deadline, `seat_needed` fires with the queue and, on a deadline, a previous-agent note.
- A push that comes back gone or dropped puts its message in the queue and fires `seat_needed`. No code path discards an inbound message.
- A person seat takes the person's DMs, group DMs and @Opal mentions. A channel seat declared in config takes every message in its channel.
- Pushed lines read `[<server>/<chat>/<sender>, <level>] ↳<thread> <text>` (thread mark only for a thread reply). `send` and `react` without ids answer the holder's latest line.
- Seat session names read `seat:<app>/<server>/@<person-slug>` and `seat:<app>/<server>/#<channel-slug>`.
- `social send` refuses a Slack DM to a person with no inbound history.
- A routine definition with `[trigger] event = "social/seat_needed"` and a filter runs once per matching event, with the payload in `AWM_ROUTINE_EVENT`. Events that arrive during a live run queue behind it.
- `routines/new-seat.toml` and its script start the agent in project `agentic-commons`, scope `seats/<person>`, with the composed brief, and ensure tags `seat` and `user:<person-slug>`. A first contact files `request_human`.
- The social, routine and new-seat tests pass.

## Tasks

- T1. Commit this plan (G1-G6)
- T2. Build the seat registry and state machine in social (G1, G2, G3)
- T3. Reshape the pushed line, session names, send defaults and the first-DM guard (G4, G5)
- T4. Compact
- T5. Add event triggers to the routine plugin (G3)
- T6. Write the new-seat routine and its script (G1, G3)
- T7. Write the general seat SOP and the server briefs (G5, G6)
- T8. Compact
- T9. Run the tests and review the diff (G1-G6)
- T10. Update the docs the build made wrong (G6)
- T11. Commit and push the branch without deploying (G1-G6)
- T12. Report to Tony and to redux-endstate/review (G1-G6)
- T13. Debrief

## Approach by task

### T1. Commit this plan

Commit `docs/seat-lifecycle/PLAN.md` on `feat/seat-pause`.

Gotchas: none.

### T2. Seat registry and state machine

Layer: social (`awm/services/social/awm/social/`). Add a seat table through a dao migration (8 to 9): one row per seat with account, key (`@<slug>` or `#<channel>`), kind, state (`empty`, `starting`, `occupied`, `retiring`), holder, `done_at`, `retiring_since`, and a queue table of inbound messages. A routing function maps an inbound message to its seat: a declared channel seat first, then the sender's person seat for a DM, a group DM or an @Opal mention, else none. `_on_message` routes, then pushes to a live holder or queues and emits `seat_needed`. A mount by the seat's agent moves `starting` to `occupied` and drains the queue. New verb `mark_seat_done`. A supervised loop handles the 4-hour reclaim (push the retire instruction), the retiring reply, the 4-minute deadline, and a stuck `starting`. The Pusher's gone and dropped paths queue the message and fire `seat_needed`.

Gotchas: the Pusher lives in gatewayclient (`push.py`) and other services use it, so add a drop callback rather than change its default. Mounts stay keyed by chat, so a person seat's agent mounts each chat it answers. The keeper's `channels = "all"` remounting must not fight the new routing: leave the keeper as is and declare no kept seats on slack-bot.

### T3. Pushed line, names, send defaults, first-DM guard

Layer: social `seats.py` and the send and react handlers. `push_line` gives `[<server>/<chat>/<sender>, <level>] ↳<thread> <text>`. `<server>` is the platform for a DM and the workspace or guild name for a channel. The Slack connector exposes the team name it already reads. `session_name` gives the `@`/`#` forms. Record each holder's latest line, so `send` and `react` fill a missing channel, thread or message id from it, and accept a chat label (`DM`, `#arbutus`). `send` refuses a Slack DM where `has_inbound` is false.

Gotchas: `social history` and agents in flight still use ids, so keep explicit ids working. Update `test_seats.py` expectations for the new line.

### T4. Compact

Handoff: the plan file, the todo list, commits for T2 and T3. A re-read cannot recover the routing order (channel seat, then person seat), the state names, or the choice to leave the keeper alone. Run `reflection compact` with followup "continue with T5".

### T5. Event triggers in the routine plugin

Layer: `awm/plugins/routine/`. `definitions.py` accepts `[trigger] event = "<service>/<topic>"` and a `[trigger.filter]` table of payload equalities. At boot the plugin subscribes to each installed routine's topic with `SupervisedSubscription`, as `operations/arbutus.py:204` does. A matching payload starts a run with `AWM_ROUTINE_TRIGGER=event` and `AWM_ROUTINE_EVENT=<json>`. The runner queues event runs behind a live run instead of refusing them.

Gotchas: social runs on alcor, so the subscription needs a peer (the `AWM_SOCIAL_PEER` pattern). A reinstall or uninstall must restart or drop the subscription.

### T6. The new-seat routine

Layer: `routines/new-seat.toml` and `scripts/seats/new-seat.py` in the awm repo. The script reads `AWM_ROUTINE_EVENT`. It ensures the `seat` and `user:<slug>` tags, composes the brief (SOP, server brief, seat facts), and calls `reflection start_other` in `agentic-commons` scope `seats/<slug>` with the seat name, not pinned. The brief tells the agent to mount its chats. A first contact files `operations request_human`. Unit tests cover brief composition and argument building with the awm calls stubbed.

Gotchas: `agentic-commons` does not exist as a project yet. The script must fail loudly, never fall back to another scope. A routine run has no session, so check that `request_human` and `start_other` accept a routine caller.

### T7. Seat SOP and server briefs

Layer: `docs/redux/drafts/agentic-commons/docs/`. Rewrite `seat-sop.md` as the general SOP: lifecycle, journaling (tags, after each result, handoff on reclaim), speak rule, trust levels, first contact, reaching Tony. Add `briefs/slack-hallam-lab.md` with the lab-specific text from the old SOP and the opal-seat-holder skill, and a `briefs/discord-unimatrix0.md` stub. Mark the drafted `opal-seat-holder` skill for retirement.

Gotchas: load `write-docs` first. Keep R125's cooperative limits (no cards, five subagents, no Tony personal info).

### T8. Compact

Handoff: commits for T5 to T7, the todo list. A re-read cannot recover which tests ran or failed. Run `reflection compact` with followup "continue with T9".

### T9. Tests and review

Run the social, gatewayclient push, routine and new-seat tests through `run-tests.sh` from this worktree. Read the whole diff once against the acceptance criteria.

Gotchas: run tests from this worktree, not the workspace root (memory: run-tests tests the cwd tree). Use `/dev/shm` basetemp if SQLite tests hang.

### T10. Docs

Update social `INSTALL.md` § Seats for states, routing, the line and `mark_seat_done`, and the routine docs for event triggers. Subtract what the change made false.

Gotchas: load `write-docs`. Edit only inside each file's stated purpose.

### T11. Commit and push

Commit each layer separately. Push `feat/seat-pause` to origin. Do not merge to dev or release, and do not run a deploy.

Gotchas: no force-push.

### T12. Report

Tell Tony in this session what landed, what the deploy needs (create the `agentic-commons` project, install the routine, declare channel seats, retire the skill, lift the pause), and what stays open. Send redux-endstate/review the one final report.

Gotchas: one message to redux-endstate/review only.

### T13. Debrief

Run the `retire` skill: debrief journal citing the decision journals, then stop.

## Callouts

- Kalen's (9e22f611) and Patrik's (a1406294) temporary seats run on today's code. They are untouched by this build until a deploy.
- Creating `agentic-commons` on GitHub and the lab fork (R128 item 1) is outward-facing and left to the deploy.
