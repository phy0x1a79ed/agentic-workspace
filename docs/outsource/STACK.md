# outsource: Discussion stack

**Goal.** Tune the outsource compute market (formerly baitan) so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/outsource/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

`node-prep`: prep other nodes so bids don't stick on a greedy optimum.

## Waiting on the user

Nothing.

## Preferences

- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.

## Stack

- `node-prep`: State: prep is just an offer, biddable placement, posted explicitly by the caller (option a); no own verb; offers stage their own data. Revised 2026-11-22: "we probably do need a prep verb"; offers can be given explicitly to a specific provider; prep = the data-transfer version of that; no count; requires use the type system so sessions don't duplicate a uniref50 transfer. Pending: confirm typed materials (reverses no-props at material level) and `to` semantics. (also: with props gone, a reference DB at different paths on two providers has two ids, so no cross-provider hit; prepping nodes with references lands here) "how other nodes can be prepped so that bids dont get stuck on a greedy optimum."
  - `remote-fetch`: "for something like uniref50, we'd want to be able to run the download remotely as well." A typed material whose source is a download run at the provider, not an rsync from the caller.
  - `globus`: "we need to consider things like globus transfer..." Transfer method between providers (clusters) other than rsync over ssh.
- `discovery`: agents can't find outsource (no AGENTS.md or skill; describe example now in catalog). Agents must pass absolute paths (plugin has no per-call cwd).
- `tune`: "I want to tune the Baitan service."
- `deploy`: try a real job live after deploy ("need to try for real"). Also build runtime stats (peak memory, CPU time) per settle, from `resources-model`. one deploy of the rename plus walkthrough changes. Migration (from the rename worker): move `~/.baitan` → `~/.outsource` and `baitan.toml` → `outsource.toml` on every node; fix link commands naming `bin/baitan`; install `outsource`, remove `baitan` binary and old socket; `BAITAN_*` → `OUTSOURCE_*`; refresh pins; restart awm. Outside the repo: metasmith nf-baitan executor still says baitan and posts raw sets, which the public surface no longer takes. JOBS.md needs `task.env` and `place.files`.
- carried from dex-poc stack (open there): `test-cases` (msm across HPC clusters), `arrays`, `arbutus-spot`, `backend-hardening`.

## Workers

None.

## Closed

- `rename`: baitan → outsource, landed 0385a219, deploys with `deploy`; scope now awm/outsource. e2e `far` failure is pre-existing and environmental.
- `call-walkthrough`: closed through its children.
- `resources-model`: fixed size as today; peak memory and CPU use are runtime statistics to gather.
- `offer-shape` (absorbs `cache-default`): transform-shaped, keyless requires/produces, protocol, .env material, stat/commit identity, no props.
- `adoption`: general compute; any heavy agent command is a candidate.
