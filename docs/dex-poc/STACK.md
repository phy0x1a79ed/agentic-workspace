# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`first-step`

## Waiting on the user

- Is the primitive resolve-a-send, with the auction as one way of choosing a provider? (Revises design section 5.)
- Which workloads does the first step carry? Recommended: one comparable pure job and one exclusive call (a read-only verb on an alcor singleton, alongside the existing path), plus `claude-md-sync`.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.

## Stack

- `first-step`: the primitive and the first workloads. Option A (in-process module host) is superseded.
  - `claude-md-sync`: CLAUDE.md across the fleet as the test of HTTP revalidation. Open: one origin per name against C3.
- `transport-survey`: do AWM peer links or the push path already give subscribe and keepalive?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `fill`: whether a build on another node is in the first step (L1-L3)
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `seam`: loader and ABI surface (Q1, Q23). Later rollout step. Survey: do the plugin contract and pins allow a dynamically linked plugin?
- `swap`: in-process swap, state loss, bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected, keepalive is the only clock.
