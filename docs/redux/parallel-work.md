# Parallel work

## Purpose & Contents

This doc holds the parallel work skill. The skill tells an agent how to plan a large task in waves across top-level sessions, when each session claims and releases a scope, and how the work is gathered and deployed. Thread 1p produces it.

Related rules live in other docs:

- `dispatch.md` R50 sets when an agent may start a top-level session.
- `sessions.md` R51 and R52 hold the wait and the paths for work that spans sessions. R56 to R58 hold names and task ids.
- `scopes.md` R2 and R42 hold how a session enters and claims a scope.

## Requirements

- **R59.** A parallel work skill guides a large task. It tells the agent how to plan in waves split across agents, when to claim and release a scope, and how to gather and deploy the work. Tony: "I think we need a parallel work skill for large tasks, that instructs to plan in waves split across agents, when to scope claim and unclaim, gathering and deploying." (2026-11-01).
  - The skill serves `sessions.md` R52 path 2. `dispatch.md` R50 step 1 names it.
  1. A wave is a set of concerns that do not depend on each other. The coordinator starts one top-level session per concern and waits on them with `reflection wait`. The wave ends with a gather: the coordinator merges the wave's branches into its own branch. The next wave starts from that merged branch, so it needs no scatter. One `deploy` follows the last wave. Tony: "This is sensible." (2026-11-01).
  2. The skill is an add-on to the planning style in the global `CLAUDE.md`. It says to plan to implement in parallel by coordinating agents, never to do the work itself, and to execute in waves. Tony: "The skill itself should be structured as an addon to planning and essentially just say plan to implement in parallel by coordinating with agents, do not work yourself. Execute in waves." (2026-11-01).
  3. The global `CLAUDE.md` points to the skill for a task with clear parallel streams of work. Tony: "the parallel work skill should be referred to by the global prompt to use if task has clear parallel streams of work" (2026-11-01).
  4. The skill stays short and repeats nothing from the planning style. Tony: "Keep it short since we dont know what needs nudging yet until it actually gets used a few times. It shoukd not he redundant with existing plan instructions" (2026-11-01).
  - The approved draft lives at `drafts/parallel-work/SKILL.md`. Install the skill on each node, with the pointer of step 3, when `reflection wait` (`sessions.md` R51) ships. Step 3 of the skill calls that verb. Tony: "Accept, next" (2026-11-01).
  - The pointer reads: "**Parallel streams.** When the task has clear parallel streams of work, load the `parallel-work` skill before you write the plan." It goes in the global `CLAUDE.md` § Planning style, after the `write-docs` paragraph.

## Gap from today

Status at the wave-2 gather (`feat/redux` 16da3979, 2026-11-04). `docs/redux-review/build-survey.md` § 3 holds the row for each requirement.

- **R59.** The skill draft sits at `docs/redux/drafts/claude-home/skills/parallel-work/SKILL.md`. T27 installs it with its pointer in `CLAUDE.md`.
