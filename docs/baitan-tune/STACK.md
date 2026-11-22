# baitan-tune: Discussion stack

**Goal.** Tune the baitan compute market so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/baitan-tune/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

`resources-model`: CPUs vs memory.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.

## Stack

- `call-walkthrough`: "walk through what an actual call would look like and how it can be fullfilled". Shape closed as `offer-shape`; build worker running; try it live once built ("need to try for real").
  - `resources-model`: CPUs vs memory. "memory is probably a floor, but memory may also increase with the number of cores. For CPUs, that's probably something more of like a metric ... some stuff just doesn't go faster with more cores." Keep apart from metrics.
- `discovery`: agents can't find outsource (no AGENTS.md, skill, or describe example).
- `node-prep`: (also: with props gone, a reference DB at different paths on two providers has two ids, so no cross-provider hit; prepping nodes with references lands here) "how other nodes can be prepped so that bids dont get stuck on a greedy optimum."
- `tune`: "I want to tune the Baitan service."
- `rename`: "I also want to rename it." Landed 0385a219 (not deployed). 43 unit tests pass on altair; plugin e2e fails on its `far` link (`ssh localhost` inside the build container) — check against pre-rename code.
- `deploy`: one deploy of the rename plus walkthrough changes. Migration (from the rename worker): move `~/.baitan` → `~/.outsource` and `baitan.toml` → `outsource.toml` on every node; fix link commands naming `bin/baitan`; install `outsource`, remove `baitan` binary and old socket; `BAITAN_*` → `OUTSOURCE_*`; refresh pins; restart awm. Outside the repo: metasmith nf-baitan executor still says baitan.
- carried from dex-poc stack (open there): `test-cases` (msm across HPC clusters), `arrays`, `arbutus-spot`, `backend-hardening`.

## Workers

- offer-shape build worker (sonnet): new offer verb, compile to task+place, stat/commit identity with dirty warning, .env via apptainer, catalog example; also checks the e2e `far` failure against pre-rename code. No deploy.

## Closed

- `offer-shape` (absorbs `cache-default`): transform-shaped, keyless requires/produces, protocol, .env material, stat/commit identity, no props.
- `adoption`: general compute; any heavy agent command is a candidate.
