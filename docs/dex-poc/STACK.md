# DEX POC: Discussion stack

**Goal.** Decide the first step of an incremental rollout of the AWM + DEX system, then hand off to a build session. The user has now directed a prototype build and an experiment report, so this session builds the POC before the handoff.

**Decisions.** `docs/dex-poc/DECISIONS.md`. **Sources.** `docs/dex-poc/awm-dex-{1-context,2-design,3-open-questions}.md`.

## Current

`compute-market`: baitan (摆摊), a compute market for awm peers, Slurm clusters and spot machines. The user's pressing issue (2026-11-11); evidence in journal 7ab424c3. Closed so far, in `DECISIONS.md`: market-scope, standalone, braavos-offer, metasmith-reuse, promise, mutation, dependencies, deliverable, cache-identity, cache-scenarios, storage, locator. Deferred items and test cases: journal 3a6c999a (tag `deferred`), edited in place as items change.

Now: `gc`. First question: how a requester marks which outputs a provider deletes once they are moved back (reference databases and build tools are exempt), and whether that is binding.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-08: Do not cut over everything at once (the lesson of the Rust redux).
- 2026-11-08: Not a POC but an incremental rollout. About 20 parts (core plugins, awm, SDK) port eventually. First step is a new market plugin for distributed compute with a well-defined narrow tool; the core stays abstract enough to absorb the existing infrastructure, including calls to alcor singletons.
- 2026-11-09: No time-based staleness.
- 2026-11-09: Full exploration including prototype builds, not only docs.
- 2026-11-09: Builds run on capella. The contract is strict versioned JSON.
- 2026-11-09: MVP is cache, update event and remote fill; bidding after.
- 2026-11-11: The plugin is named baitan (摆摊).
- 2026-11-12: Tests mock jobs (CheckM, builds) rather than run them.
- 2026-11-12: baitan never hashes content; identity is declared properties (promises).
- 2026-11-11: No content hashing of bytes; identity is lineage and type.
- 2026-11-11: The market is standalone, callable without awm.
- 2026-11-11: "pressing issue is some compute market for awm peers."
- Standing: one report at the end, no interim status reports.

## Stack

- `compute-market`: see Current.
  - `gc`: "indicate what to delete once moved back, which can omit reference databases and build tools".
  - `fronting`: altair bids on behalf of providers that cannot run a live service (HPC clusters) or sleep (spot machines for GTDB-Tk and metabuli, images set up by shogun, spun up by altair). Each machine is its own provider and its own bid; a front submits several bids.
  - `providers`: awm peers and Slurm clusters (sockeye, fir, nibi, trillium) behind one abstraction. Largely absorbed by `fronting`.
  - `fair-share`: Slurm fair-use quotas as an allocation input, "as a system".
  - `tool-surface`: the narrow exposed tool, roughly submit, status, result, cancel. A 15 to 50 minute build cannot be one blocking call.
  - `bids`: what a bid vector holds, how bids move as a provider reaches capacity, the scoring algorithm.
  - `redundancy`: with one reliable and one flaky provider, run at least as fast as the reliable one alone, with the flaky one helping occasionally.
  - `build-repro`: do real workloads (kb, lance, musl release) give byte-identical output? E4 tested only a dependency-free crate.
  - `nextflow`: run compiled Nextflow workflows through the market. Aspirational; last under `compute-market`. Includes test 2 of journal 3a6c999a (checker CheckM must hit the plan's CheckM cache).
- `envelope-mapping`: map the dex TypeScript kernel's envelope (altair, dex/edge) to `{v, id, from, to, selector, args}`; ask whether both share one envelope.
- `first-test`: `claude-md-sync` is a candidate after the experiments. One origin per name against C3 is still open.
- `version-policy`: whether a reply is translated to the caller's version (caller sends an `accept` list; receiver replies in the highest version it can produce, else a structured error).
- `transport-survey`: do AWM peer links or the push path already give subscribe?
- `core-boundary`: the core crate against the AWM adapter. Includes whether awm's catalog-to-CLI/MCP/HTTP generator moves to an awm-free crate.
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
- `market-scope`: a general job (build first); HPC clusters as providers; standalone; Nextflow aspirational.
- `standalone`: three generated surfaces; shell, awm agents, Nextflow as a whole job.
- `braavos-offer`: what + who; lineage identity; offer carries axes, picker and checker; reviews; size feedback.
- `metasmith-reuse`: take the ideas into a new tool; no fork. Renamed baitan.
- `promise`: offer, bid, award, started, settled; broadcast events plus pollable remote state; review after the checker.
- `mutation`: one kind of offer; facts as triples of type sets; offer sets bound to one provider; no query engine yet.
- `dependencies`: layer 1 exact-match offer keys; type-agnostic core; exact dependency names; types as optional facts.
- `deliverable`: named output slots; compute ends at L; delivery is a separate placement step.
- `cache-identity`: declared property sets; id names, subset match finds; hits are provider bids.
- `cache-scenarios`: reviews score, never fail; broken promise = never delivered; no invalidation.
- `storage`: stores are places, placement is an offer; chinook bids placement only; transfer inside the compute bid; requester states current input places.
- `locator`: not needed; consumers know their inputs and request outputs moved back.
- `poc-build`: E1 to E4 run; results in `FINDINGS.md`.
