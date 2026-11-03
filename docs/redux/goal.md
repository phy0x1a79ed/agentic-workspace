# Goal

## Purpose & Contents

This doc holds why awm exists and the end state Tony wants from awm, in his words, the tiers his work falls into, and the core loop that ties them together. Every other area doc serves this loop.

Status: draft from thread 1. It waits for Tony's confirmation.

## Why awm exists

awm is the infrastructure for how Tony works with agents across machines. Tony is the bottleneck. awm queues issues and tasks for him in one line, so he no longer tracks them himself. It holds his non-linear ideas until a session can pursue them. It turns his intent into concrete steps, runs them in parallel, keeps private work and lab work apart, and keeps what each run learns.

Tony: "awm is the infrastructure for how I interact with agents with across machines. It is intended to streamline the main bottleneck (me and my inconsistent motivation and energy) by organizing issues and tasks linearly to the main user operator so that I dont have to go looking and track all the issues myself (which is getting impossible). Kindof like hyperthreading a CPU by some perspectives. It also lets me have non-linear ideas and off-load them to be pursued later. It then converts al of this to a traceable agent orchestration system that breaks down my intent into concrete actionable steps, distributes them for parallel work, handles private and work boundaries, and accumulates wisdom and experience so that agentic, deterministic, and human effort is not wasted" (2026-11-02).

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
