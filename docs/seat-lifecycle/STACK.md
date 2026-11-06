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

**lost-pushes**. Its result: `lost-pushes/README.md`.

## Stack

- **lost-pushes** (2026-11-06): it looks like there is a bug in how seats work. retired seats came back
  - [ ] What happens when a push fails or finds its holder gone? Recommend: social treats the seat as having no live holder and emits the message to the new-seat routine, which starts a fresh agent with it. No message is dropped. The build reads alcor's social.log to confirm Steven's case.
- **rc-toggle** (2026-11-06): about the seats, it would be great if on the new fleet status dashboard, I can turn remote control on and off for seats
  - [ ] How is a seat marked in the roster that list_other and claudedaemon report (pin plus trust, or a role tag)? Asked by trilium-design/interview.
  - [ ] Is turning Remote Control off and on for a seat passkey-gated (sessions R68), or free inside an open window? Asked by trilium-design/interview.
  - [ ] Tony's question 'can we sort natively in claude?' as it bears on seats. Asked by trilium-design/interview.
- **unpause** (2026-11-06): can we pause and shutdown seats on altiar?
- **edge-cases** (2026-11-06): the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else. ... slack main handles those edges cases and uses judgement. can also ask me (users.md R126, 2026-11-02)
- **seat-brief** (2026-11-06): need to consolidate previous decsions into brief for seats on altair. need to also have a more general version for other seats like discord

## Parked

## Closed

- **lifecycle** (closed 2026-11-06): `lifecycle/README.md`
- **resume-from-journals** (closed 2026-11-06): `resume-from-journals/README.md`
- **journal-tags** (closed 2026-11-06): `journal-tags/README.md`
- **speak-rule** (closed 2026-11-06): `speak-rule/README.md`
