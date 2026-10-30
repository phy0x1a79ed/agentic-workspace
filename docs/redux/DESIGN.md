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
2. A new session takes the task. It routes itself to a scope, and creates the scope when none fits.
3. Sessions run in parallel. Each comes back with a report, or with a blocker or question.
4. Returns join one pending queue. A separate list shows the active work.
5. Tony picks items from the queue and acts on each:
   - He accepts a report. The session closes with a summary journal.
   - He answers a question. The session continues.
   - He dispatches new tasks. They report back to the asking session and resume it.

Every interface is a channel onto this loop. awm needs three channels: chat with humans, chat with agents of other systems, and a tether to machines.

## Requirements

### Routing and scopes (thread 1a)

- **R1.** A new session routes itself. Tony may name a scope, but the session picks the scope and enters it. It creates the scope, or the project, when none fits. Its system prompt tells it to name itself and find its home first.
- **R2.** A session enters a scope only through `reflection cd`. The Claude Code harness blocks `cd` in some cases. The single entry point also lets awm enforce R3.
- **R3.** A scope is one git worktree and holds at most one live session. A session is live while it works, while it is blocked on a question, and while it waits for review.
- **R4.** An agent may create a project, which is a git repo. Example: "try out this new tool `<github link>`" clones the tool as a project and enters a branch as a scope.
- **R5.** A scope outlives its sessions. It stays until a session removes it. awm never deletes a scope when a session ends, because the worktree can hold DVC pins and uncommitted files. Tony: "At least Im not organized enough yet to allow this automation."
- **R6.** A hygiene check finds scopes not in use and cleans them up. It triggers infrequently, for example every 10 new scopes, and acts only when action is needed. It is not an agent that churns on each new scope. Tony does not have to remember to run it.

- **R7.** A new session can need a scope that a live session occupies. When the scope clearly fits its task, the new session acts as follows:
  1. It asks Tony on the pending queue.
  2. It waits in a queue for the scope.
  3. awm notifies it when its turn comes. It enters the scope.
  4. It appends to its question that the conflict is now clear. It never withdraws the question. Tony: "these events may cause me to reconsider the scope layout and ask for new scopes."

**CAUTION** Today `reflection cd` does not refuse an occupied scope, and `start_other` refuses only a duplicate session name. R3 and R7 are not built yet.

## Architecture

(open)

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now."
- 2026-10-30: New work goes to a new session, never into a live session's context. Tony: "I feel this encourages better performance from agents." See R3 for a scope that a live session occupies.
- 2026-10-30: A session closes with a summary journal. A result that is not good gets a new session, not a restart of the old session with its full context.
- 2026-10-30: Agents use subagents freely. They create top-level sessions only with Tony's permission.
- 2026-10-30: Seats are sessions. A seat is a session on a channel.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Source: precedence `c6bea06ada542338`.
