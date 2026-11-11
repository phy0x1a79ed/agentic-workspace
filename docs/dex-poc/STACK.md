# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. The user has now directed a prototype build and an experiment report, so this session builds the POC before the handoff.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`compute-market`: a compute market for awm peers. The user's pressing issue (2026-11-11). Evidence: journal 7ab424c3 (cold builds on mizar, full disk from per-worktree targets, contention) and E4 in `FINDINGS.md`. First question: what the first version covers.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: Builds run on capella. The contract is strict versioned JSON.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after.
- 2026-11-11: "pressing issue is some compute market for awm peers."
- Standing: one report at the end, no interim status reports.

## Stack

- `compute-market`: see Current.
  - `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel. A 15 to 50 minute build cannot be one blocking call.
  - `bidding`: allocation among peers (requirement predicate, bid, award).
  - `build-repro`: do real workloads (kb, lance, musl release) give byte-identical output? E4 tested only a dependency-free crate.
- `envelope-mapping`: map the dex TypeScript kernel's envelope (altair, dex/edge) to `{v, id, from, to, selector, args}`; ask whether both share one envelope.
- `first-test`: `claude-md-sync` is a candidate after the experiments. One origin per name against C3 is still open.
- `version-policy`: whether a reply is translated to the caller's version (caller sends an `accept` list; receiver replies in the highest version it can produce, else a structured error).
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `swap`: state handover and bad-activation recovery (Q7, Q8, Q24). Later rollout step.
- `handoff`: the brief for the build session, written from the findings

## Workers

None.

## Closed

- `freshness`: event-driven; subscribed or disconnected.
- `keepalive`: dropped from the design; a socket option.
- `mvp-scope`: cache, update event, remote fill first; bidding after.
- `exploration-direction`: full exploration with prototype builds on a closed system.
- `contract-and-builds`: strict versioned JSON; capella builds; mizar and capella as the pair.
- `format-per-locality`: struct in process, JSON from IPC outward.
- `calling-mechanism`: working design, taken as accepted when the user directed the build.
- `poc-build`: E1 to E4 run; results in `FINDINGS.md`.
