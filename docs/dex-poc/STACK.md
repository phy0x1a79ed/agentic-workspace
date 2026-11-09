# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`mvp-poc`

## Waiting on the user

- Where does the POC live? Recommended: a standalone Cargo workspace in this scope, with nodes as processes on loopback, no `awm-plugin-sdk`. The AWM plugin adapter comes after the MVP works.
- Is resolve-a-send still the primitive, now that bidding is deferred? (Revises design section 5.)

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after. Work test cases one at a time as the system builds up.

## Stack

- `mvp-poc`: a hands-on build of cache, update event and remote fill. Open: where it lives, and the primitive.
  - `first-test`: which test case goes first. Candidate: `claude-md-sync` (data only, no compiler). One origin per name against C3 is still open.
- `transport-survey`: do AWM peer links or the push path already give subscribe and keepalive?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `bidding`: market allocation. Deferred until the MVP works.
- `seam`: loader and ABI surface (Q1, Q23). Later rollout step.
- `swap`: in-process swap, state loss, bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected, keepalive is the only clock.
- `mvp-scope`: cache, update event, remote fill first; bidding after; test cases one at a time.
