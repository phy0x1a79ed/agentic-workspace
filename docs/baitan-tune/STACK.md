# baitan-tune: Discussion stack

**Goal.** Tune the baitan compute market so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/baitan-tune/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

`call-walkthrough`: one real call end to end; how its dependencies move.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.

## Stack

- `call-walkthrough`: "walk through what an actual call would look like and how it can be fullfilled... as in how all the dependencies move over." Gap found: the `task` op stages data but has no model for the software environment. State: environment is a material (Tony, 2026-11-22). Decided 2026-11-22: model the offer on a metasmith transform declaration (requirements, products, protocol), but outsource and metasmith stay distinct tools; no data-type registry. Keyless (2026-11-22): `requires` is a list of paths; `produces` is paths copied back; no named slots ("I don't think model should then become this universal reference across all time"). Identity: metasmith-style stat ids, no content hashing. Open: role of props vs requires; folder whose nested file changes without the top mtime moving (git repo test).
  - `resources-model`: CPUs vs memory. "memory is probably a floor, but memory may also increase with the number of cores. For CPUs, that's probably something more of like a metric ... some stuff just doesn't go faster with more cores." Keep apart from metrics.
  - `cache-default`: path-identified materials go stale when a file is edited; may be moot if transforms carry metasmith lineage.
- `node-prep`: "how other nodes can be prepped so that bids dont get stuck on a greedy optimum."
- `tune`: "I want to tune the Baitan service."
- `rename`: "I also want to rename it." Decided: "renaming everything from baitan -> awm/outsource"; worker running.
- carried from dex-poc stack (open there): `test-cases` (msm across HPC clusters), `arrays`, `arbutus-spot`, `backend-hardening`.

## Workers

- rename worker (sonnet): baitan → outsource across the awm repo, on feat/baitan-tune; no deploy.

## Closed

- `adoption`: general compute; any heavy agent command is a candidate.
