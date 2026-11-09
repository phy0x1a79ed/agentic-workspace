# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`closed-system`

## Waiting on the user

- Drop the transport keepalive from the design? It is a socket option, not a design item, and nothing in the MVP acts on it.
- Run it in two phases: A, standalone hosts on mizar and alcor over plain TCP; B, the same host as a plugin under a dev gateway on each node?
- Start the frozen contract as one C-ABI entry point, opaque bytes across it, and OCI-style digests (immutable) with tags (mutable names)? (From my knowledge of OCI and CLAP, not from the docs; check before adopting.)
- Dispatch the first worker on the hot-swap host (Phase A, task 1)? Model and effort to confirm.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after. Work test cases one at a time as the system builds up.

## Stack

- `closed-system`: two nodes (mizar, alcor), module L (a copy, loaded in-process) and service S (a service, routed). Iterate the contract, caches, eviction and remote resolution against it.
  - `hot-swap`: cdylib host, content-hash filenames, swap under load; state across the swap accepted as lost first
  - `copy-vs-service`: a name resolves to a copy (by hash, any holder, cacheable, evictable) or a service (a live provider, routed, never copied). The provider declares which.
  - `frozen-contract`: the floor, and whether an existing standard fits
  - `cache-eviction`: eviction applies to copies only; a service route is invalidated by events
  - `remote-resolution`: fill from a peer by hash; route to a service; reconnect and catch up
  - `first-test`: `claude-md-sync` is a candidate once the above exists. One origin per name against C3 is still open.
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `bidding`: market allocation. Deferred until the MVP works.
- `seam`: loader and ABI surface (Q1, Q23). Now partly inside `hot-swap` and `frozen-contract`.
- `swap`: state handover and bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected, keepalive is the only clock.
- `mvp-scope`: cache, update event, remote fill first; bidding after; test cases one at a time.
