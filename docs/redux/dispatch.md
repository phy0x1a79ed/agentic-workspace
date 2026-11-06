# Dispatch

## Purpose & Contents

This doc holds how work starts for an issue that nobody planned for. It holds who decides and what authority a recorded decision of Tony's carries. Thread 1k produces it.

Timer and agent triggered processes belong in `routines.md`. How a session starts, closes and raises follow-up issues belongs in `sessions.md`.

## Requirements

- **R49.** Agents hold the judgment. awm stays deterministic. awm never matches an issue against precedence itself. Tony: "agens hold the intelligence here, awm is deterministic" (2026-10-31).
  - R49 states a given, not a protocol to build. It stays here as the record that the thread considered it. Tony: "this is a given. it doesnt need to be explicitly recorded as some protocol... but i guess leaving some record to indicate that we've thought this out is useful".
  1. When something unexpected comes up in a session, the agent deals with it as it sees fit. It uses the tools available, precedence among them. Tony: "in an agentic session, something unexpected comes up -> then the agent deals with it as it sees fit using the tools available like precedent."
  2. When a routine meets something unexpected, the routine raises an exception, because routines are deterministic. The exception starts an agent (`routines.md` R47), and step 1 applies. Tony: "exceptions route to agentic sessions, so what I just said applies."
- **R50.** An agent may start other top-level sessions without Tony's permission. Guidelines in its brief govern when. Tony: "Yes, I've realized that subagents are insufficient and ill suited for the type of parallel dev work i want. So agents can spawn other agents without my permission." (2026-11-01).
  1. The agent plans parallel implementation in waves, with the parallel work skill (thread 1p), when the task decomposes into substantial parallel concerns. Tony: "We can give guidelines like plan for parallel implementation in waves (using the skill) if the task can be decomposed into substantial parallel concerns."
  2. It starts a top-level session when the plan calls for it, when an unexpected discovery needs a new concern dealt with, or when Tony asks. Tony: "Then spawning top level agents is fine when the plan calls for it, when an unexpected discovery requires a new concern to be dealt with, or when i ask."
  3. When unsure, it asks Tony first. His answers go into precedence until they show when a start is fine. Tony: "We can err on the side of asking until we build up some precedent for when it is ok to spawn top level agents." Precedence `eea2745af7b0c9bf` records the rule.
  - Each top-level session has the full awm tooling, which a subagent lacks. Tony: "This enables true parallel sessions that each has full awm tooling."
  - Subagents stay for exploring and for parallel implementation inside one scope. Tony: "Sub agents are reserved for things like explore and parallel implementation tasks that can haplen" and "... that can happen in a single scope." (2026-11-01).
  - A routine exception still starts its agent through the `routine` service (`routines.md` R47).
  - The guidelines live in the brief. Code cannot enforce them. Tony: "R50 is not enforceable by code since permission can be given through dialogue" (2026-10-31).
- `sessions.md` R12 step 4 supplies the input: Tony's decisions on follow-up issues go into precedence.

## Decisions

- 2026-11-01: Precedence does not change for dispatch. Agents keep using it as they do today. Tony: "precedence doesnt need to change? why is this a subject? I didnt ask to talk about this..."

## Gap from today

Status at the wave-2 gather (`feat/redux` 16da3979, 2026-11-04). `docs/redux-review/build-survey.md` § 3 holds the row for each requirement.

Nothing in this doc is open. R49 is a given, and the lifecycle brief carries the R50 guidelines.
