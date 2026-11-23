# outsource: Discussion stack

**Goal.** Tune the outsource compute market (formerly baitan) so agents doing compute-heavy work (metasmith sessions on altair) actually use it, and rename it.

**Decisions.** `docs/outsource/DECISIONS.md`. **Prior design.** awm `feat/dex-poc`, `docs/dex-poc/DECISIONS.md`.

## Current

Executing plan /home/tony/.claude/plans/abstract-munching-magpie-of-b55cc06f-c89c-4147-bf15-d0c8c81ebce5.md (T1–T17; the todo list tracks progress). T1–T4 done: release 5.1 = awm release 98c26809 (feat/outsource 3fefbe58), live on mizar, alcor, shogun, altair; capella and lyra asleep, take it at next refresh. Live-check: altair offer `to: fir` settled (o.txt = fc30554). T6 done 2026-11-23: nibi and trillium are providers on altair (hand-added `[[cluster]]` blocks; trillium whole_node, node_cpus 192, walltime caps 24 h); test offers settled on c474.nibi and tri0089. Masters are awm `ssh` scopes with ControlPersist no: they live until the connection drops, and `ssh connect` reopens them through the 2fa service with no human. T7 done: nf-outsource on metasmith feat/engine/outsource-executor (ba2190aa, d513332e, unpushed), stub tests green on altair (build copy ~/.cache/nf-outsource-build; JDK ~/.local/jdk/jdk-21.0.13+9; nextflow 26.04.1 in msm env). Never run live. Config: plugins nf-outsource@0.1.0, process.executor='outsource', outsource{node='altair'}, ext.outsource=[to:..]. T9 done (deneb held): alcor's telemetry watch folded into cloud inventory (e32194e); mizar and alcor applied, rerun changed=0; cloud dev and release at 225740c, pushed github. T10 done: a cache-hit rerun reused yesterday's instance; a fresh build (--locked) settled on altair (wall 42 s, cpu 83 s, rss 412 MB). Release 5.2 = awm release 7d4ed8ad (feat 0bf4fb21): local jobs drop the gateway egress proxy (cargo got CONNECT 403), a break reason carries the log tail. T11 and T12 running.

Found during T4, for T14:
- An offer `to: fir` posted on mizar got `NoBids`; the same offer on altair settles. mizar's entry may not reach altair's provider host.
- The outsource `offer` verb's `metrics` param description still says default ["time"]; it is now time + waste.
- The ship merge trailers must sit in the last paragraph with Co-Authored-By, or `%(trailers)` misses them.
- nf-outsource re-implements the client `compile()` (OfferSet, stat ids, .env, task+place steps) in Groovy, because the entry accepts only `{set}`; drift breaks it silently. Fix: entry accepts the transform shape, or compile moves to a shared crate.
- `resources` has no time field; the plugin sends task time as `spec.est`.
- nf-outsource unverified live: status `{promise, phase}` shape, `from` routing after reconnect, dispose, a `place` to the caller's node, `-resume` session id.
- Found in T10/T11 (2026-11-23):
  - First offer of a large unseen material (metasmith/release, 187 MB) after mizar's market starts drops every link at once (alcor, altair, shogun) and fails NoBids; the identical repeat works, and a 3000-file folder does not trigger it. Seen at 11:00:15 and 11:23:13 UTC.
  - 12 shards on one node each `apptainer pull` the same 4 GB image at once: /tmp fills ("no space left on device"), 10 break. Fix: one pull per image under a lock, APPTAINER_TMPDIR off /tmp.
  - Two shards staging one material on trillium race: `find` saw the other's rsync temp file ("cannot stage"). Fix: one stage per material at a time.
  - robust-z picker amplifies a small time spread among HPC bids (~1090 s): trillium's 92 s lead (z 1.35) beats its clamped waste penalty (-1), so a whole-node cluster wins 4-core... 2-cpu jobs. Picker design question.
  - Offers from mizar never reach an HPC: "no source for input" (push goes only along mizar's edges, altair does not relay to its clusters).
  - `resources` without `mem_mb` is rejected (BadArgs) though the schema says it defaults.
  - Instance size counts the whole work dir (107 MB for a 1.3 MB binary): confirmed again.
- A dead slot is requeued only for groups with no `#sbatch` line (cac78807); a slot that dies after `sbatch` ran but before echo can still double-submit. Accepted.

## Waiting on the user

- `deneb-entry` (paged 2026-11-23): deneb carries board/relay/genai drift from other scopes, and a provider there breaks fleet R79 (public box calls out to nothing; tree test pins its plugin set). Options: drop, do anyway (amend R79 + test), defer. Recommended drop or defer.

## Preferences

- 2026-11-22: The real try posts with no `to`: "it should pick the best node, which prolly wont be the slurm ones given the queue delay".
- 2026-11-22: General compute, not workflow-only.
- 2026-11-22: Deploy once, with the rename plus every change decided in the walkthrough.
- 2026-11-22: Offer = materials (paths, some folders) + method (a bash command) + metrics to bid against.
- dex-poc, carried: no persistent outsource process on HPC; fix broken ssh rather than work around it.
- 2026-11-22: Keep `prep` and `import`: "they cover real steps in the user story". After the solver build and the v26 test-trim run, debrief; Tony reviews the lift in the next session.

## Stack

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
