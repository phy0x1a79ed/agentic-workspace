# Dispatch

## Purpose & Contents

This doc holds how work starts for an issue that nobody planned for. It holds who decides, what authority a recorded decision of Tony's carries, how his decisions reach precedence, and how he sees and corrects the result. Thread 1k produces it.

Timer and agent triggered processes belong in `routines.md`. How a session starts, closes and raises follow-up issues belongs in `sessions.md`.

## Requirements

- **R49.** Agents hold the judgment. awm stays deterministic. awm never matches an issue against precedence itself. Tony: "agens hold the intelligence here, awm is deterministic" (2026-10-31).
  - R49 states a given, not a protocol to build. It stays here as the record that the thread considered it. Tony: "this is a given. it doesnt need to be explicitly recorded as some protocol... but i guess leaving some record to indicate that we've thought this out is useful".
  1. When something unexpected comes up in a session, the agent deals with it as it sees fit. It uses the tools available, precedence among them. Tony: "in an agentic session, something unexpected comes up -> then the agent deals with it as it sees fit using the tools available like precedent."
  2. When a routine meets something unexpected, the routine raises an exception, because routines are deterministic. The exception starts an agent (`routines.md` R47), and step 1 applies. Tony: "exceptions route to agentic sessions, so what I just said applies."
- **R50.** An agent never starts a top-level session on its own. A recorded decision in precedence that covers the case does not count as Tony's permission. The agent asks Tony on the board, as thread 1e requires. Tony: "no, agents still shouldnt be able to start top level agents autonomously. That had a runaway effect before" (2026-10-31).
  - A routine exception is the one automatic start (`routines.md` R47). The `routine` service starts that agent, not another agent.
- `sessions.md` R12 step 4 supplies the input: Tony's decisions on follow-up issues go into precedence.

## Decisions

None yet.

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source and the live precedence archive on altair.

- **No automatic dispatch.** Only `cx start` starts a background session (`cx/lifecycle.py`). Its callers are Tony's Discord `/new-agent`, an agent's `reflection start_other`, and seeded starts (`cx/seed.py`). Each start comes from a person or an agent turn. No event, condition or decision starts one.
- **Precedence is read by agents, never by awm.** Agents search it before they ask Tony a preference question. No service reads it to act.
- **Decisions are free text.** A decision is a triple of context, question and decision, with tags, notes and votes (`precedence/db.py`). It has no field for an issue class, an action to take, a target scope, or a scope of authority. Search ranks by embedding relevance, recency and votes.
- **Every decision is an agent's paraphrase.** The archive holds 105 active decisions, and each has source `agent`. Agents write them on Tony's behalf, usually with his quote inside. None are written by Tony or captured from his answer directly.
- **Answers on the board are not recorded.** An answer to `operations request_human` reaches the asking session only. Nothing adds it to precedence unless that session does.
- **The scrapers stage candidates only.** `precedence/scrape.py` and `scrape_transcripts.py` mine memories, operator posts, journals and transcripts into a manifest that a person curates before import. They never write the archive.
- **Schedulers do not start agents.** The `events` service emits `schedule.tick` and `agent.wake`, and only the game bot consumes them. The `routine` service of `routines.md` R34, which tests conditions and dispatches agents, is designed but not built.
- **A service can start a session.** `cx start` accepts a call with no calling session, as from a service or an operator at the front door (`_refuse_unknown_caller`, `cx/lifecycle.py`). Such a start records no parent session, so R13 lineage is lost (`routines.md` R47 traces it through the run). A node with the `station` role refuses every start (R48 names the peer instead). `cx start` also needs a fixed project and scope (`sessions.md` R18 conflict).
- **R50 is a convention only.** No code enforces it. An agent session can call `cx start` or `reflection start_other` with no approval from Tony. The lifecycle brief that `cx start` adds names who to report to and grants or forbids nothing.
