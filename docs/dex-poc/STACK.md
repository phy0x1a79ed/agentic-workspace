# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. The user has now directed a prototype build and an experiment report, so this session builds the POC before the handoff.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`poc-build`: build the POC and run four experiments, in order. Report findings once, at the end, as `docs/dex-poc/FINDINGS.md` and in one reply. No interim status reports.

Where it lives: `poc/` in this worktree, a standalone Cargo workspace (an empty `[workspace]` table, no dependency on `awm-plugin-sdk`). Sync the sources to capella and build there with native `cargo` (glibc 2.35, so artifacts also load on mizar and alcor at 2.39). mizar has no `cargo` on its PATH. Capella is the second node, reached by `ssh capella` stdio, with no listening port. Keep capella scratch under a job-unique `/tmp` name and delete it afterwards. Do not touch the production gateways; the baseline uses read-only calls only.

Experiments:
- **E1 `calling-mechanism`.** One call site, three placements: a `cdylib` loaded in process (struct path), a separate process on mizar (Unix socket, JSON), a process on capella (ssh stdio, JSON). Uniform async surface, deadline, closed error set. Kill the callee and expect `unavailable` through the same code path. Record p50 and p99 per placement. Baseline: the existing gateway `POST /invoke` locally and with `peer=capella`, for a trivial read-only verb.
- **E2 `update-flow`.** Hot swap module L v1 to v2 under constant load, with content-hash filenames: zero failed calls and results change once, in order. Capella builds v2 and publishes it. mizar gets the publish event over the link, fetches the bytes by digest, verifies, loads and swaps. A corrupt build and a panic-on-init build are rejected and v1 keeps serving. Show state lost across the swap. A node offline during a publish catches up on reconnect by sending the digest it holds. Answer: where does an update originate, where does the new version come from, and does it start from a binary.
- **E3 `cache-pagetable`.** A content-addressed store per node. The per-node router table, name to `Copy{digest}` or `Service{route}`, is the candidate page table. The origin's tag table is authoritative; each node's table is a cache invalidated by events. Cap the store at 3 artifacts. Evict copies only, never a loaded module or a service route. An evicted copy refills on its next call. Report where the table lives and what survives a restart.
- **E4 `market-build`.** mizar posts a work item (source digest, toolchain, target triple, glibc baseline, requirement) to both nodes. mizar cannot compile and declines. capella bids with an expected duration, mizar accepts, capella builds, and mizar fetches the artifact by digest. Rebuild on capella and compare output hashes: report whether cargo output is byte-reproducible.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: Builds run on capella. The contract is strict versioned JSON.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after.
- 2026-11-09: "compact and build POC, report experiment findings."
- Standing: one report at the end, no interim status reports.

## Stack

- `poc-build`: see Current.
- `first-test`: `claude-md-sync` is a candidate after the experiments. One origin per name against C3 is still open.
- `version-policy`: whether a reply is translated to the caller's version (caller sends an `accept` list; receiver replies in the highest version it can produce, else a structured error).
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel
- `core-boundary`: the core crate against the AWM adapter
- `coexistence`: how it runs in a few select sessions (Q30) and what stays out
- `bidding`: market allocation beyond the build job. Deferred.
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
