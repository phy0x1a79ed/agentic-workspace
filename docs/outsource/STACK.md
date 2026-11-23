# outsource: Discussion stack

**Goal.** Tune the outsource compute market (formerly baitan) so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/outsource/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

`deploy`: apply the decided changes and try a real job. Tony 2026-11-22: "lets deploy outsource and have the test trim as another test case". Next steps, in order:
1. Wait for the slurm-hang worker; confirm all outsource crate tests and the plugin e2e (except the known `far` link) pass.
2. Load the `ship` skill; merge feat/outsource to dev and run the release train.
3. Node migration (list under `deploy` in Stack); avoid capella (never wake it); lyra is unreachable.
4. Real job 1: metasmith solver build (`src/workflow_solver`), no `to`. Only altair can build it today (cargo+musl, apptainer); Slurm fronts can bid only with a Rust/musl `.env`.
5. Real job 2: test-trim shards on HPC (`test-trim-hpc`).

## Waiting on the user

Nothing.

## Preferences

- 2026-11-22: The real try posts with no `to`: "it should pick the best node, which prolly wont be the slurm ones given the queue delay".
- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.
- dex-poc, carried: no persistent outsource process on HPC; fix broken ssh rather than work around it.
- 2026-11-22: Keep `prep` and `import`: "they cover real steps in the user story". After the solver build and the v26 test-trim run, debrief; Tony reviews the lift in the next session.

## Stack

- `deploy`: "I just meant to now apply these changes and try it for real". Shipped release dbec46b1 (2026-11-22); nodes migrated to ~/.outsource; capella refreshed by the rollout but not migrated (still ~/.baitan); lyra asleep. Live check passed. First real job (solver build, rust-musl .env, no `to`) got NoBids: only altair has apptainer and it declines "no source for input": providers pull, and no node can reach mizar. Tony chose push-along-edges (b) over authorizing keys. Release 2 = dex-poc merge + push + provider-fit, then rerun the solver build. Leftover: refresh-pins.sh exited 0 on a cargo --locked failure; market gives up redialing a link whose binary is missing during rollout; e2e `far` needs ssh in the build container.
  - `push-edges`: when X cannot reach holder H but H reaches X, H pushes. Worker running.
  - `provider-fit`: decline when job cpus/mem exceed the provider's; scale est-less time by cpus. deneb (2 cpus, 7 GB, 34 GB disk) stays a provider: "good for long running tasks that require high up time" — an uptime axis is deferred until a job needs it. Worker running.
- `test-trim-hpc`: "can metasmith v26 bug test trim use outsource to run the metasmith tests in parallel on some other, prolly HPC provider". Session metasmith-v26-bugs-test-trim (altair, metasmith/engine/test-trim) is itself asking Tony how Sockeye fits (its recommendation: release tier, no docker; smoke/merge stay local). Coordinated 2026-11-22 (message sent to that session over Remote Control; read not confirmed): plan its HPC lane as outsource offers, wait for deploy, report breakage to awm/outsource. Second real job after deploy: one offer per test shard, metasmith .env container, repo as material, junit back.
- absorbed from dex-poc (session "dex compute" retired 2026-11-22; its STACK/DECISIONS/FINDINGS at docs/dex-poc/ on feat/dex-poc, merged by the push worker):
  - `test-cases`: "Want to try msm job distributed across hpc clusters, not just 1".
  - `arrays`: mock submissions show one array per (cpus, mem) profile per cluster, sized by the burst, no cap or throttle; a second profile waits one gate; trillium's whole_node gives each task a whole node. Open question to Tony: which go in before the live HPC test — pack tasks on trillium, submit all profiles per ssh call, cap arrays with a % throttle.
  - `arbutus-spot`: "we can think about offering spot general compute instances on arbutus as well".
  - `backend-hardening`: no timeout on a hung ssh master freezes that cluster's scheduler; ssh dropping right after a good sbatch marks running jobs failed; failed scancel not retried; one extra ssh per finished job and per import for its listing; unused `awm` feature in the plugin.

## Workers

- push worker: merge feat/dex-poc (stateless altair fronts bb75af6c, never shipped), then `push-edges` and `provider-fit`; commits on feat/outsource.

## Closed

- `slurm-test-hang`: mock squeue counted zombies as running under the container's non-reaping init; fixed 441b291c.

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
