# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`calling-mechanism`

## Waiting on the user

- What carries IPC and network calls in the POC: a direct framed JSON link, or the existing gateway path? (Recommended: direct link, with the gateway path measured as the baseline.)

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: Builds run on capella. The contract is strict versioned JSON.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after. Work test cases one at a time as the system builds up.
- 2026-11-09: Work the four scenario topics one at a time with dfs-interview, in the user's order.

## Stack

- `closed-system`: nodes mizar and capella (alcor later as a node that never compiles); module L (a copy, loaded in-process) and service S (routed). Capella builds and publishes L.
  - `calling-mechanism`: cross-plugin calls in process, over IPC and over the network, from one call site. Tests the calling mechanism.
  - `update-flow`: a network call, a stale notice, a cross-network update. Where the update comes from, where the new version comes from, and whether it starts from a binary.
  - `cache-pagetable`: how the cache works and where the "page table" lives. Copies against services; eviction.
  - `market-build`: a build job between mizar and capella through a market bit.
  - `version-policy`: whether a reply is translated to the caller's version (caller sends an `accept` list; receiver replies in the highest version it can produce, else a structured error).
  - `test-play`: rebuild the play from the decisions above.
- `first-test`: `claude-md-sync` is a candidate after the play. One origin per name against C3 is still open.
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `bidding`: market allocation beyond the build job. Deferred.
- `swap`: state handover and bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected, keepalive is the only clock.
- `keepalive`: dropped from the design; a socket option.
- `mvp-scope`: cache, update event, remote fill first; bidding after; test cases one at a time.
- `exploration-direction`: full exploration with prototype builds on a closed system.
- `contract-and-builds`: strict versioned JSON; capella builds; mizar and capella as the pair.
- `format-per-locality`: two formats, struct in process and JSON from IPC outward.
