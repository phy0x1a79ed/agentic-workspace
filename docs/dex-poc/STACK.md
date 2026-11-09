# DEX POC: Discussion stack

**Goal.** Decide what POC plugin to build for the AWM + DEX design, then hand off to a build session. This session does not implement it.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`poc-claim`

## Waiting on the user

- Is option A the claim: a module-host plugin that succeeds if a module edit recompiles only that module and goes live in the running plugin without a restart?

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: This session's deliverable is the decision, the scope, the docs moved to the scope, and a handoff for the build session.

## Stack

- `poc-claim`: what this POC has to prove. Options: A module-host plugin (recommended), B fill plugin, C gateway re-expression (design section 14).
- `seam`: the loader and ABI surface (Q1, Q23). First survey: do the plugin contract and pins allow a dynamically linked plugin?
- `first-module`: which selectors or real workload it hosts
- `swap`: put/activate, state loss, bad-activation recovery (Q7, Q8, Q24)
- `fill`: whether a build on another node is in scope (L1-L3)
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `handoff`: the brief for the build session

## Workers

None.

## Closed

None.
