# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`test-play`

## Waiting on the user

- Is the six-act play the thing to build against? (Acts: cold fill, copy vs service, live swap, versioned calls, eviction, catch-up.)
- Reply version policy: the caller sends an `accept` list and the receiver replies in the highest version it can produce from that list, else a structured error?
- Dispatch the first worker on Acts 1 and 2 (a host on mizar and capella, resolution, fill)? Model and effort to confirm.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: Builds run on capella. The contract is strict versioned JSON.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after. Work test cases one at a time as the system builds up.

## Stack

- `closed-system`: nodes mizar and capella; module L (a copy, loaded in-process) and service S (routed). Capella builds and publishes L.
  - `test-play`: the six-act scenario to build against, worked one act at a time
    - act 1 `cold-fill`: first call fills L from capella by digest, loads it, answers
    - act 2 `copy-vs-service`: S is routed and never copied; kill and restart S
    - act 3 `live-swap`: publish L v2 under load, bad build rejected, in-process encoding cost measured
    - act 4 `versioned-calls`: v1 calls still work after v2 goes live; unknown keys rejected
    - act 5 `eviction`: cache cap evicts copies only, never a loaded module or a service route
    - act 6 `catch-up`: a node offline during a publish catches up on reconnect, with no polling
  - `frozen-contract`: decided JSON, strict, versioned. Open: reply version policy, in-process fast path.
  - `first-test`: `claude-md-sync` is a candidate after the play. One origin per name against C3 is still open.
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `bidding`: market allocation. Deferred until the MVP works.
- `swap`: state handover and bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected, keepalive is the only clock.
- `keepalive`: dropped from the design; a socket option.
- `mvp-scope`: cache, update event, remote fill first; bidding after; test cases one at a time.
