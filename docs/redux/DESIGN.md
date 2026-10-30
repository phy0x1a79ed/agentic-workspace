# awm redux: design

## Purpose & Contents

This doc holds the ground-up architecture for awm and the fleet. It records only what Tony and the interview settled. Each section closes one thread of `TO-DISCUSS.md`.

- **Goal** — the end state Tony wants, in his words.
- **Requirements** — what must hold for the goal to be met.
- **Architecture** — the shape that meets the requirements.
- **Decisions** — each settled choice, with its date and Tony's words.

Earlier drafts are inputs, not the frame. They are cloud/tencent `FLEET.md` (07d0856), awm/feat-fleet-board `FLEET-BOARD.md` (cbf805d7) and awm/infra-core-redux `docs/design/core-redux.md` (cae36f2b, on altair only). Their concepts, such as circles and a control node, enter this doc only when a thread re-derives them.

## Goal

Draft from thread 1. It waits for Tony's confirmation.

Tony: "the fundamental goal here is using awm to leverage what I can do" (2026-10-30).

The work has three tiers. Tony bends their boundaries to get things done faster, and awm must tolerate that.

- **Project** — a set of workstreams that progress in parallel and serve one cohesive purpose. Projects relate to each other. metasmith is several independent projects joined by its orchestrator.
- **Scope** — a persistent workstream. It owns one contained feature or task. It is well defined, but it can be open ended, such as research.
- **Session** — one agent run that advances a scope.

The core loop runs like promises:

1. Tony states a task, an idea or an experiment as it comes up. He does not name the scope.
2. An agent routes the task to a scope, and creates the scope when none fits. A new session takes the task.
3. Sessions run in parallel. Each comes back with a report, or with a blocker or question.
4. Returns join one pending queue. A separate list shows the active work.
5. Tony picks items from the queue and acts on each:
   - He accepts a report. The session closes with a summary journal.
   - He answers a question. The session continues.
   - He dispatches new tasks. They report back to the asking session and resume it.

Every interface is a channel onto this loop. awm needs three channels: chat with humans, chat with agents of other systems, and a tether to machines.

## Requirements

(open)

## Architecture

(open)

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now."
- 2026-10-30: New work on a scope with an active session goes to a duplicate of the scope with its own session, not into the active session. Tony: "I feel this encourages better performance from agents."
- 2026-10-30: A session closes with a summary journal. A result that is not good gets a new session, not a restart of the old session with its full context.
- 2026-10-30: Agents use subagents freely. They create top-level sessions only with Tony's permission.
- 2026-10-30: Seats are sessions. A seat is a session on a channel.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Source: precedence `c6bea06ada542338`.
