# Dispatch

## Purpose & Contents

This doc holds how work starts for an issue that nobody planned for. It holds who decides and what authority a recorded decision of Tony's carries. Thread 1k produces it.

Timer and agent triggered processes belong in `routines.md`. How a session starts, closes and raises follow-up issues belongs in `sessions.md`.

## Requirements

- **R49.** Agents hold the judgment. awm stays deterministic. awm never matches an issue against precedence itself. Tony: "agens hold the intelligence here, awm is deterministic" (2026-10-31).
  - R49 states a given, not a protocol to build. It stays here as the record that the thread considered it. Tony: "this is a given. it doesnt need to be explicitly recorded as some protocol... but i guess leaving some record to indicate that we've thought this out is useful".
  1. When something unexpected comes up in a session, the agent deals with it as it sees fit. It uses the tools available, precedence among them. Tony: "in an agentic session, something unexpected comes up -> then the agent deals with it as it sees fit using the tools available like precedent."
  2. When a routine meets something unexpected, the routine raises an exception, because routines are deterministic. The exception starts an agent (`routines.md` R47), and step 1 applies. Tony: "exceptions route to agentic sessions, so what I just said applies."
- **R50.** An agent never starts a top-level session on its own. A recorded decision in precedence that covers the case does not count as Tony's permission. The agent asks Tony on the board, as thread 1e requires. Tony: "no, agents still shouldnt be able to start top level agents autonomously. That had a runaway effect before" (2026-10-31).
  - A routine exception is the one automatic start (`routines.md` R47). The `routine` service starts that agent, not another agent.
  - R50 stays a rule in the brief. Code cannot enforce it, because Tony can give permission in dialogue. Tony: "R50 is not enforceable by code since permission can be given through dialogue" (2026-10-31).
- `sessions.md` R12 step 4 supplies the input: Tony's decisions on follow-up issues go into precedence.

## Decisions

- 2026-11-01: Precedence does not change for dispatch. Agents keep using it as they do today. Tony: "precedence doesnt need to change? why is this a subject? I didnt ask to talk about this..."

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source.

- **No automatic dispatch.** Only `cx start` starts a background session (`cx/lifecycle.py`). Its callers are Tony's Discord `/new-agent`, an agent's `reflection start_other`, and seeded starts (`cx/seed.py`). Each start comes from a person or an agent turn. No event, condition or decision starts one.
- **Schedulers do not start agents.** The `events` service emits `schedule.tick` and `agent.wake`, and only the game bot consumes them. The `routine` service of `routines.md` R34, which tests conditions and dispatches agents, is designed but not built.
- **A service can start a session.** `cx start` accepts a call with no calling session, as from a service or an operator at the front door (`_refuse_unknown_caller`, `cx/lifecycle.py`). Such a start records no parent session, so R13 lineage is lost (`routines.md` R47 traces it through the run). A node with the `station` role refuses every start (R48 names the peer instead). `cx start` also needs a fixed project and scope (`sessions.md` R18 conflict).
- **No brief states R50.** The lifecycle brief that `cx start` adds names who to report to. It does not tell the agent to ask Tony before it starts a top-level session.
