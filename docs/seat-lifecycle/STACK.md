# seat-lifecycle: stack

## Purpose

Decide how Slack seats work after the 2026-11-06 incident: when a seat starts, speaks and retires; how it journals; how a task resumes from journals; and how Tony controls seats (pause, Remote Control) from the fleet dashboard.

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

**edge-cases**. Its result: `edge-cases/README.md`.

## Stack

- **unpause** (2026-11-06): can we pause and shutdown seats on altiar?
- **edge-cases** (2026-11-06): the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else. ... slack main handles those edges cases and uses judgement. can also ask me (users.md R126, 2026-11-02)
  - [x] Drop slack-main entirely and give its jobs to the routine? Recommend: every chat is a seat with its own agent (a DM per person, a channel per channel, started only on an @Opal mention); a first DM from a new person notifies Tony and starts the agent; a sender outside the lab gets no agent, and the routine holds the message and asks Tony. (answered 2026-11-06)
  - [x] Key a seat by person instead of by chat? Recommend yes: every message from one person (DM, group DM, @Opal in a channel) goes to that person's one agent, which replies where the message came from. A channel message with no @Opal starts nothing. (answered 2026-11-06)
  - [x] Seat name format: person seat seat:<app>/<server>/@<person-slug>, channel seat seat:<app>/<server>/#<channel-slug>, with the same <person-slug> as scope seats/<person> and tag user:<person>? Recommend yes. (answered 2026-11-06)
  - [ ] Pushed line '[DM] Steven Chen: <text>' (or '[#arbutus] Tony: ...', '[DM, thread]' for a thread reply), with social filling the ids: send and react from a seat holder default to the chat, thread and message of the latest inbound line, and take the chat label ('DM', '#arbutus') when the agent answers elsewhere? Recommend yes.
- **seat-brief** (2026-11-06): need to consolidate previous decsions into brief for seats on altair. need to also have a more general version for other seats like discord

## Parked

## Closed

- **lifecycle** (closed 2026-11-06): `lifecycle/README.md`
- **resume-from-journals** (closed 2026-11-06): `resume-from-journals/README.md`
- **journal-tags** (closed 2026-11-06): `journal-tags/README.md`
- **speak-rule** (closed 2026-11-06): `speak-rule/README.md`
- **lost-pushes** (closed 2026-11-06): `lost-pushes/README.md`
- **seat-states** (closed 2026-11-06): `seat-states/README.md`
- **rc-toggle** (closed 2026-11-06): `rc-toggle/README.md`
