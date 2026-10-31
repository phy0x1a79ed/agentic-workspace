# Goal

## Purpose & Contents

This doc holds the end state Tony wants from awm, in his words, the tiers his work falls into, and the core loop that ties them together. Every other area doc serves this loop.

Status: draft from thread 1. It waits for Tony's confirmation.

## End state

Tony: "the fundamental goal here is using awm to leverage what I can do" (2026-10-30).

## Work tiers

The work has three tiers. Tony bends their boundaries to get things done faster, and awm must tolerate that.

- **Project** — a set of workstreams that progress in parallel and serve one cohesive purpose. Projects relate to each other. metasmith is several independent projects joined by its orchestrator.
- **Scope** — a persistent workstream. It owns one contained feature or task. It is well defined, but it can be open ended, such as research.
- **Session** — one agent run that advances a scope.

## Core loop

The loop runs like promises:

1. Tony states a task, an idea or an experiment as it comes up. He does not name the scope.
2. A new session takes the task. It routes itself to a scope, and creates the scope when none fits.
3. Sessions run in parallel. Each comes back with a report, or with a blocker or question.
4. Returns join one pending queue. A separate list shows the active work.
5. Tony picks items from the queue and acts on each:
   - He reads a report. The session closed on its own with its journal.
   - He answers a question. The session continues.
   - He dispatches new tasks. They report back to the asking session and resume it.

## Channels

Every interface is a channel onto the core loop. awm needs three channels: chat with humans, chat with agents of other systems, and a tether to machines.
