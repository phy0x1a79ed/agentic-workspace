# outsource: Discussion stack

**Goal.** Tune the outsource compute market (formerly baitan) so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/outsource/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

Executing plan /home/tony/.claude/plans/abstract-munching-magpie-of-b55cc06f-c89c-4147-bf15-d0c8c81ebce5.md (T1–T17; the todo list tracks progress). T1–T14 done. Next: T15 Simplify (outsource-slurm, proto, CLI, provider and market changes; nf-outsource), T16 docs, T17 debrief.

Releases: 5.1 98c26809, 5.2 7d4ed8ad, 5.3 8fa50dde, 5.4 64f6f4ec (feat/outsource 1fa95564). Live on mizar, alcor, altair, shogun; capella and lyra asleep, take it at next refresh.

T14 (2026-11-23) fixed: folder identity hashes the tree listing, a dirty repo's id includes its dirt (warnings field gone); `resources` fields default; one apptainer pull per image under flock, on the provider host (cluster login node) before the job, with APPTAINER_TMPDIR/CACHEDIR under the provider root; one stage per material (flock + rename); Slurm tasks write `<workdir>/log`, so breaks carry a tail; instance size counts produces only; a directed round waits up to 300 s for its provider, late bids log as late; the attach/connect bridge exits when either side closes (the "first big offer drops every link" bug: a remote plugin restart left a bridge on a dead socket until the next write).

Reruns on 5.4: T11 12 shards from altair, no `to`, all 5 providers bid on time (trillium waste 0.99), altair won all on time (42 s vs ~1050 s HPC), junit for every shard, same 9 environmental failures, wall 7 min 18 s. T12 unhinted spread by itself: altair 14, nibi 3, fir 3, 1 min 2 s. T12 hinted over five providers: 4 each on altair, fir, nibi, sockeye, trillium, ~1 min. Kits: altair ~/.cache/outsource-t11 (round4.sh), ~/.cache/outsource-t12 (run.sh, spread5.config).

Still open from the tests (stacked below as `t14-left`): picker vs waste (Tony); offers from mizar never reach an HPC (no relay through altair); nibi settles with no run stats; nf-outsource duplicates the client compile and has no time field; the ship trailer must sit in the last paragraph; a slot that dies after `sbatch` can double-submit (accepted).

## Waiting on the user

- `picker-waste` (paged 2026-11-23): robust-z lets trillium's small time lead beat its waste penalty. (a) ratio/log time scoring, recommended; (b) waste weight ~2; (c) decline waste > 0.9 unless directed.
- `deneb-entry` (paged 2026-11-23): deneb carries board/relay/genai drift from other scopes, and a provider there breaks fleet R79 (public box calls out to nothing; tree test pins its plugin set). Options: drop, do anyway (amend R79 + test), defer. Recommended drop or defer.

## Preferences

- 2026-11-22: The real try posts with no `to`: "it should pick the best node, which prolly wont be the slurm ones given the queue delay".
- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.
- dex-poc, carried: no persistent outsource process on HPC; fix broken ssh rather than work around it.
- 2026-11-22: Keep `prep` and `import`: "they cover real steps in the user story". After the solver build and the v26 test-trim run, debrief; Tony reviews the lift in the next session.

## Stack

- `t14-left`: mizar→HPC relay (altair does not pass mizar's pushes on to its clusters); nibi run stats; nf-outsource shared compile (entry accepts the transform shape, or a shared crate) and a time field.
- `deploy`: "I just meant to now apply these changes and try it for real". Shipped release dbec46b1 (2026-11-22); nodes migrated to ~/.outsource; capella refreshed by the rollout but not migrated (still ~/.baitan); lyra asleep. Live check passed. First real job (solver build, rust-musl .env, no `to`) got NoBids: only altair has apptainer and it declines "no source for input": providers pull, and no node can reach mizar. Tony chose push-along-edges (b) over authorizing keys. Release 2 = dex-poc merge + push + provider-fit, then rerun the solver build. Leftover: refresh-pins.sh exited 0 on a cargo --locked failure; market gives up redialing a link whose binary is missing during rollout; e2e `far` needs ssh in the build container.
  Release 2 d45d926d (dex-poc merge e2aba1b1, push 7c56bf47, fit 04445929). Real job 1 SETTLED 2026-11-22 20:59: altair won (49 s bid vs capella 169 s; 4 nodes declined on cpus), wall 60 s, cpu 81 s, rss 412 MB, binary back on mizar. First attempt broke: protocol built inside the staged repo (skill now says outputs go in the work dir, ee1c3d94). altair now hosts fir + sockeye ([[cluster]], routes); nibi/trillium need a Duo master. mizar has no cluster masters. Instance size reported 107 MB for one 1.3 MB file (whole target dir?).
  - `topology`: release 4 dfa5a2b9 live (DECISIONS `topology`). Verified 2026-11-23: mizar offer (cpus 8) → altair; altair-entry offer `to: fir` ran on fir node fc30554 and returned. Cloud: feat/outsource merged to cloud dev a8455a5, pushed github; NOT applied and cloud `release` not promoted. Check-mode showed mizar = reorder only; alcor would lose a hand-added `[telemetry] watch` line (not in inventory: fold it in before applying); deneb carries other scopes' unapplied board/relay drift and gets no outsource (station package). altair is outside site.yml (arbutus.yml `--tags altair` is guarded), hand-enabled stays. capella: entry on next awm_config apply when awake, never woken. lyra asleep. Log noise: entries redial capella/deneb/lyra every 60 s.
    - `deneb-entry`: deneb as a provider ("good for long running tasks that require high up time"): home outsource on deneb in plugins.yml, adjust deneb's plugin-set test (host_vars/deneb.yml), apply once deneb's pending drift is settled by its owner.
  - was: `topology` (release 4): see DECISIONS `topology`. Found 2026-11-23: altair has no edge to mizar, so altair agents could not reach the single mizar market.
  - closed `caller-site` (release 3 898fc1a4, superseded in part by `topology`): the plugin takes the caller's node and cwd from its own process (mizar), so altair callers (test-trim) are read on mizar; use the gateway `_caller_*` stamp and probe identity on the caller node. Bundled: status keeps broken-award reasons (NoBids hid "made none of its outputs"); market redials links forever; refresh-pins exit code.
  - closed `push-edges`, `provider-fit` (deneb stays a provider: "good for long running tasks that require high up time"; uptime axis deferred).
- `test-trim-hpc`: told 2026-11-23 via journal post 9d93cb9b in metasmith/engine/test-trim (its Remote Control session is gone); awaits its run and Tony's approval of its plan.
   "can metasmith v26 bug test trim use outsource to run the metasmith tests in parallel on some other, prolly HPC provider". Session metasmith-v26-bugs-test-trim (altair, metasmith/engine/test-trim) is itself asking Tony how Sockeye fits (its recommendation: release tier, no docker; smoke/merge stay local). Coordinated 2026-11-22 (message sent to that session over Remote Control; read not confirmed): plan its HPC lane as outsource offers, wait for deploy, report breakage to awm/outsource. Second real job after deploy: one offer per test shard, metasmith .env container, repo as material, junit back.
- absorbed from dex-poc (session "dex compute" retired 2026-11-22; its STACK/DECISIONS/FINDINGS at docs/dex-poc/ on feat/dex-poc, merged by the push worker):
  - `test-cases`: "Want to try msm job distributed across hpc clusters, not just 1".
  - `arrays` → closed as `slurm-vanilla` 2026-11-23; was: mock submissions show one array per (cpus, mem) profile per cluster, sized by the burst, no cap or throttle; a second profile waits one gate; trillium's whole_node gives each task a whole node. Open question to Tony: which go in before the live HPC test — pack tasks on trillium, submit all profiles per ssh call, cap arrays with a % throttle.
  - `arbutus-spot`: "we can think about offering spot general compute instances on arbutus as well".
  - `backend-hardening`: no timeout on a hung ssh master freezes that cluster's scheduler; ssh dropping right after a good sbatch marks running jobs failed; failed scancel not retried; one extra ssh per finished job and per import for its listing; unused `awm` feature in the plugin.

## Workers

None.

## Closed

- `slurm-vanilla` (from dex-poc `arrays`): 10 s gate, one call does all due, waste axis soft limit.

- `capella-provider`: opportunistic provider when awake.

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
