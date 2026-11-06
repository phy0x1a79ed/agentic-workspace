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

**tony-dm**. Its result: `tony-dm/README.md`.

## Stack

- **tony-dm** (2026-11-06): Open from the plan: Tony's own DM with Opal (D0C9GMGRSFP, trusted). Is it a person seat like the others, where does its agent run, and what can it reach?
  - [x] Tony's DM is a person seat like the others (seat:slack/slack/@tony, trusted, agentic-commons seats/tony, same start and 4-hour reclaim), with R67/R68 in its brief? Recommend yes. (answered 2026-11-06)
  - [ ] Notifications stay bot posts in the Discord DM and start nothing; when Tony writes, his seat's agent reads the DM's recent history, notices included, so a reply to a notice has its context? Recommend yes.
- **keeper** (2026-11-06): Open from the plan: the social keeper keeps long-lived seats from social.toml [seat.*] (Tony's Discord DM butler, #bots, Hallam general). Does the new lifecycle replace it?
- **group-dms** (2026-11-06): Open from the plan: a group DM with two lab members routes each sender to their own person seat, so two agents may answer in one chat.

## Parked

## Closed

- **lifecycle** (closed 2026-11-06): `lifecycle/README.md`
- **resume-from-journals** (closed 2026-11-06): `resume-from-journals/README.md`
- **journal-tags** (closed 2026-11-06): `journal-tags/README.md`
- **speak-rule** (closed 2026-11-06): `speak-rule/README.md`
- **lost-pushes** (closed 2026-11-06): `lost-pushes/README.md`
- **seat-states** (closed 2026-11-06): `seat-states/README.md`
- **rc-toggle** (closed 2026-11-06): `rc-toggle/README.md`
- **edge-cases** (closed 2026-11-06): `edge-cases/README.md`
- **seat-brief** (closed 2026-11-06): `seat-brief/README.md`
- **unpause** (closed 2026-11-06): `unpause/README.md`
- **seat-model** (closed 2026-11-06): `seat-model/README.md`
- **channel-seats** (closed 2026-11-06): `channel-seats/README.md`
- **seat-nodes** (closed 2026-11-06): `seat-nodes/README.md`
- **seat-start** (closed 2026-11-06): `seat-start/README.md`
