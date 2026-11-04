---
name: parallel-work
description: An add-on to planning for a task with clear parallel streams of work. Plan the work as waves of top-level sessions that you coordinate, and do none of it yourself. Load it before you write the plan.
---

# Parallel work

Plan to implement in parallel by coordinating agents. Do not do the work yourself. Execute in waves.

This skill adds to the planning style in the global `CLAUDE.md`. Keep that structure and add what follows.

## Plan

- Give each stream of work to one top-level session, a worker.
- Put workers that do not depend on each other in one wave. A later wave starts from the merged work of the waves before it.
- Add a **Waves** section after **Tasks**. Name each wave's workers and the scope each one owns.
- State the contracts that workers share, such as an interface or a file format, so that each worker can work alone.
- Make each wave's gather a task of its own.

## Run a wave

1. Start one worker per stream with `reflection start_other`. Pass your task id. Name each worker `<task>/<descriptor>`.
2. Brief each worker with its stream, its contracts and its scope. Tell it to claim the scope with `reflection cd` and to work on the scope's branch. Tell it to skip the deploy, then journal and retire. Its claim ends when it retires.
3. Compact. Then call `reflection wait` with `until` set to the workers' session ids.
4. Gather. Read each worker's journal. Check its branch against its stream. Merge each branch into your feature branch. Rerun the tests.
5. Start the next wave from your feature branch.

## Land

Deploy once for each project the work touched, after the last wave. Keep every deploy and every request to Tony with you.
