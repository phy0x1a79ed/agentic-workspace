# outsource: Discussion stack

**Goal.** Tune the outsource compute market (formerly baitan) so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/outsource/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

`deploy`: apply the decided changes and try a real job.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.

## Stack

- `deploy`: "I just meant to now apply these changes and try it for real". Order: prep worker landed 1f763a67 → ontology build → runtime stats → docs (AGENTS.md sentence, outsource skill on every node, JOBS.md) → ship → node migration → real job. First real job (Tony): "rust build for the metasmith solver" — `src/workflow_solver` (msm_solver, edition 2024, path deps on sibling crates solver_witness{,_audit}, musl target per build.sh). Requires the metasmith repo root as one material (a worktree's `.git` is a file; identity.rs uses `.exists()`, so it counts). Also build runtime stats (peak memory, CPU time) per settle, from `resources-model`. one deploy of the rename plus walkthrough changes. Migration (from the rename worker): move `~/.baitan` → `~/.outsource` and `baitan.toml` → `outsource.toml` on every node; fix link commands naming `bin/baitan`; install `outsource`, remove `baitan` binary and old socket; `BAITAN_*` → `OUTSOURCE_*`; refresh pins; restart awm. Outside the repo: metasmith nf-baitan executor still says baitan and posts raw sets, which the public surface no longer takes. JOBS.md needs `task.env` and `place.files`.
- `slurm-test-hang`: `cargo test -p outsource-slurm` hung for 3–4 h on altair in two runs (test binary `outsource_slurm-…` never exits); containers stopped 2026-11-22. Fix before ship; earlier workers skipped or misread slurm results.
- carried from dex-poc stack (open there): `test-cases` (msm across HPC clusters), `arrays`, `arbutus-spot`, `backend-hardening`.

## Workers

- ontology+stats build worker (sonnet): vocabulary, `property_add`, runtime stats per settle, prep in-flight dedup test. No deploy.

## Closed

- `rename`: baitan → outsource, landed 0385a219, deploys with `deploy`; scope now awm/outsource. e2e `far` failure is pre-existing and environmental.
- `tune`: means apply the changes and try for real; folded into `deploy`.
- `discovery`: one AGENTS.md sentence (MCP tools list) + an `outsource` skill; threshold ~10 min or 4 cores; no hook. Written with deploy.
- `ontology`: market-held vocabulary, descriptions, optional allowed values, unknown keys rejected, `property_add`, lowercase values. Build queued after prep worker.
- `node-prep`: closed through its children.
- `globus`: rsync only natively; agents may run Globus themselves; `import` verb registers material already at a provider.
- `remote-fetch`: download = directed compute offer with typed produces; requires by type alone; `source` recipes deferred.
- `prep-verb`: prep verb = directed transfer offer; `to` runs a one-provider round; materials may carry a `type`.
- `call-walkthrough`: closed through its children.
- `resources-model`: fixed size as today; peak memory and CPU use are runtime statistics to gather.
- `offer-shape` (absorbs `cache-default`): transform-shaped, keyless requires/produces, protocol, .env material, stat/commit identity, no props.
- `adoption`: general compute; any heavy agent command is a candidate.
