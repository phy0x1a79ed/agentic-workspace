# Plan: redux-routines, the routine service and the deploy path

## Context

The redux designs a `routine` service that runs deterministic recurring processes, dispatches agents on a condition or a failure, and drives `deploy`, which lands a scope's work on `release` and refreshes every live process. Nothing of it exists today: deploys race, schedulers dispatch no agents, and every node updates by hand. This build writes the service and the deploy path as code with tests, runnable under today's Python gateway, and proves the deploy end to end in a temporary directory. It moves no repo, installs no timer and runs no real deploy.

## What you said

> Your lane: routines.md, all of it: routines, timer-triggered dispatch, continuous integration and deploys.

> The build container and the Rust core are redux-core's (job a92d02bf). Build the routine service and the deploy path against its plugin contract, and keep them runnable under today's Python gateway until it exists.

> Do not move the canonical repo or run a deploy: write the code and the tests, and dry-run the deploy in a temp dir only.

> Local updates only, bring the peers up to date in one sweep when we are done

## Issues

- I1. No registry lists routines, and no service tests a condition and dispatches an agent (gap T11, X2).
- I2. A failed recurring process starts no agent and tells nobody (R47, R48).
- I3. Timed work runs on three separate schedulers, none of which watches a run to its verdict (T11, T12).
- I4. No lock or queue guards a merge into `dev` or `release`, so deploys stack and race (T1).
- I5. Each submission reruns the same checks, and no check has a time budget (T4, T5, R21).
- I6. A deploy acts on one node, and other nodes update by hand (T3).
- I7. No project declares how its live processes refresh, so every awm deploy restarts the whole gateway (T15, R33).
- I8. `deploy` returns nothing a caller can track (S19, R26).
- I9. Suites that already fail on `release` are rediscovered by hand (T6).
- I10. The redux-core plugin contract does not exist yet, so a new plugin has no manifest to fill.

## High-level goals

- G1. Any project can register a routine that runs on a timer or a call, and whose failure starts an agent to fix it.
- G2. One deploy call per scope lands the work through `dev` to `release` without races or repeated checks.
- G3. Every node refreshes after a release moves, one at a time, through the project's own refresh hook.
- G4. The new service runs under today's gateway now and moves onto the Rust plugin loader without a rewrite.
- G5. The awm project itself carries the definitions it needs: its deploy config, its smoke check and its full test suite routine.

## Acceptance criteria

- A routine definition file in a project repo loads, validates and lists through `routine list`.
- `routine trigger` runs a routine's steps, records the run, and refuses a second live run of the same routine.
- `routine cancel` stops a live run and its child processes.
- A condition step that holds dispatches the routine's agent. A failed step dispatches the fixing agent on the declared peer. When that start fails, the operator DM is tried. When the DM fails, the failure is logged locally. Tests cover each branch.
- `routine schedule` writes a systemd user timer and service pair whose service calls `trigger`. `routine unschedule` removes them. Tests run against a temporary unit directory with a fake `systemctl`.
- `deploy` with no arguments resolves the calling scope, pins its head and returns a ticket id at once.
- Submissions that wait together run as one batch: one merge onto `dev`, one smoke check under a 60 second budget, one fast-forward of `release`, one refresh.
- Each failure names its step (pin, merge, check, check timeout, promote, refresh) and dispatches one implementation session with the batch's branches.
- The refresh step runs the homebase first, then each other node in order, and stops at the first failed node.
- A changed plugin folder that declares its own refresh hook refreshes alone, without the whole-gateway restart.
- The service folder carries a plugin manifest beside today's `run.sh`, and `run-tests.sh routine` passes.
- A dry-run script takes two feature branches through a full deploy in a temporary bare repo, and prints each submitter's mark.

## Tasks

- T1. Write and commit this plan (G1–G5).
- T2. Propose the plugin manifest fields this lane needs to redux-core and redux-plugins (G4).
- T3. Build the routine definitions, registry and run store (G1).
- T4. Build the runner with conditions, single live runs, cancel and the failure chain (G1).
- T5. Build the systemd timer writer behind `schedule` and `unschedule` (G1).
- T6. Wire the `routine` service into today's gateway with its manifest and tests (G1, G4).
- T7. Commit a checkpoint.
- T8. Compact.
- T9. Build the deploy engine: tickets, batches, the six steps and the failure dispatch (G2).
- T10. Build the refresh hooks and the fleet route (G3).
- T11. Write awm's deploy config, smoke check with test selection, and full suite routine (G2, G5).
- T12. Dry-run a deploy end to end in a temporary directory (G2, G3).
- T13. Commit a checkpoint.
- T14. Compact.
- T15. Draft the doc updates and the metasmith release routine in this plan (G5).
- T16. Debrief.

## Approach by task

### T1. Write and commit this plan

Write this file and commit it on `feat/redux-routines`.

Gotchas: the area docs under `docs/redux/` belong to the interview session. Edit only this draft.

### T2. Propose the plugin manifest

Send redux-core (a92d02bf) and redux-plugins (b4dbaa5a) the fields this lane reads from a plugin manifest: the plugin name and version, its services with their `run` entry and tool names, and a `refresh` hook command with its kind (`restart`, `queue` or `hot`). Ask which file name they settle on. Use `plugin.toml` meanwhile, and keep the reader in one module so a rename is one edit. Do not wait for the answer.

Gotchas: a field name chosen here and another chosen there would split the contract. Keep the reader tolerant of extra keys.

### T3. Build the routine definitions, registry and run store

New dist `awm/services/routine/` (`awm.routine`). A routine is one TOML file at `routines/<name>.toml` in its project's repo (R36). It names its triggers (`timer` as a systemd `OnCalendar` expression, `call` as a flag), its ordered steps (each a shell command with a timeout, marked `check` or `condition`), its agent for a holding condition, its fixer (peer, scope and prompt), and its report text. The registry is SQLite in the service's own DB, as `events` does: one row per installed routine with its project, path and the commit it was read at, and one row per run with its trigger, its caller session, its state, each step's outcome and the dispatched session.

Gotchas: the definition names no node (R36). The node is where it is installed. A definition read at a commit must keep that commit, so a later edit needs a reinstall, as R116 item 2 pins plugins.

### T4. Build the runner

The runner executes steps in a subprocess group with the gateway client's cancel token, so `cancel` kills the whole tree. A per-routine lock in the DB enforces one live run (R38). A `condition` step that exits 0 dispatches the routine's agent. A failed step stops the run and starts the failure chain of R48: `reflection start_other` on the declared peer, with `started_by` naming the routine and run, then the operator DM through `social send`, then a local log line. The run records the fixer's job id, and the fixer's prompt names the run, so lineage traces agent → run → fixer (R47). The outbound calls go through one small `Dispatcher` interface, so tests replace it.

Gotchas: a service start records no parent session today (dispatch.md gap), so `started_by` carries the lineage until R13 lands. The chain must never raise into the run loop.

### T5. Build the systemd timer writer

`schedule` writes `awm-routine-<project>-<name>.timer` and `.service` into the user unit directory, where the service calls `awm routine trigger`. It runs `systemctl --user daemon-reload` and `enable --now` through an injectable runner. `unschedule` disables and removes both. The unit directory is configurable.

Gotchas: build rule 2 forbids systemd changes on any host tonight. Tests use a temporary directory and a fake `systemctl`. Nothing runs against the real user manager.

### T6. Wire the service into today's gateway

Add `hub_adapter.py` with the verbs `install`, `uninstall`, `list`, `trigger`, `cancel`, `schedule`, `unschedule`, `runs`, plus the deploy verbs of T9. Add `run.sh`, `pyproject.toml`, `service.toml` and `plugin.toml`. Add the dist to `run-tests.sh`. Run its tests.

Gotchas: the gateway discovers a service by its `run.sh`. Adding the folder adds the service on the next deploy of any node, so keep the module import-safe and free of side effects at import.

### T7. Commit a checkpoint

Commit the service with its tests.

### T8. Compact

Handoff: the plan on disk, T1–T7 done, the service at `awm/services/routine/`, the T2 answer if one arrived. Next is T9.

### T9. Build the deploy engine

A `deploy` verb takes no required argument. It resolves the calling scope from the caller's working directory, or takes `project` and `scope` from a peer call (R31). It pins the branch head and returns a ticket id at once (R26). One worker per project drains the queue: every ticket waiting when a batch starts joins it, with no cap (R21). The steps follow R27: pin checks each commit exists, merge runs in a private temporary worktree of the bare repo and moves `dev` with a compare-and-swap `update-ref`, check runs the project's smoke command with a 60 second budget, promote fast-forwards `release`, refresh runs the hooks of T10, and report posts each submitter a mark to their scope's journal. A failure dispatches one implementation session, given every branch in the batch, to fix and resubmit (R23). Later batches keep landing meanwhile. The project's deploy config lives at `deploy.toml` in its repo.

Gotchas: only this engine writes `dev` and `release` (R29), so a ref that moved under it is a conflict to report, not to overwrite. A checked-out `dev` worktree goes stale when the ref moves. Fast-forward it only when it is clean. The card half of the R26 verdict needs a board verb that does not exist yet. Post to the journal and leave the card adapter as a stub.

### T10. Build the refresh hooks and the fleet route

The refresh step reads the project's `[refresh]` from `deploy.toml`: `restart` runs a command, `queue` dispatches a supervising session (R32, until the task queue exists), and `hot` diffs the old and new `release`, then runs the `refresh` hook of each changed plugin folder that declares one (R33). A change outside every plugin folder falls back to the project hook. The fleet route lists nodes homebase first. It runs the local hook, then calls `routine refresh` on each peer in turn through the gateway's peer call. It stops at the first failure and dispatches a session (R31).

Gotchas: no node is reachable tonight under build rule 2. Tests fake the peer call. The canonical repo stays on altair (T2 gap). The node list is config, not code.

### T11. Write awm's deploy config, smoke check and full suite routine

Add `deploy.toml` at the repo root, naming the smoke check, the refresh kind and the node order. Add a `--changed <base>` mode to `run-tests.sh` that maps changed paths to dists, and a `--smoke` mode that selects the `smoke` marker (T4, T5). Add `routines/full-tests.toml`, a nightly timer routine that runs every dist and records which commit passed which dist in a known-failures file, so later runs flag only new failures (R30, T6).

Gotchas: few tests carry the `smoke` marker today. A smoke run over an empty selection must pass, not fail. Pytest exit code 5 means no tests collected.

### T12. Dry-run a deploy

Add `scripts/deploy-dryrun.sh`, which builds a temporary bare repo with `dev`, `release` and two feature branches, a smoke check and a recording refresh hook, then drives the deploy engine in-process with faked dispatch and peers. Run it, then check `release` holds both branches and the hook ran once per node in order. Run the failure case: a conflicting branch dispatches one session and leaves `release` unmoved.

Gotchas: the script must never touch `projects/awm/.bare` or a real worktree. It takes its root from `mktemp -d` only.

### T13. Commit a checkpoint

Commit the deploy engine, awm's config and the dry run.

### T14. Compact

Handoff: T1–T13 done, the dry-run output, open questions to the coordinator. Next is T15.

### T15. Draft the doc updates and the metasmith release routine

Add a section to this plan with the proposed area doc edits: the routines.md gap lines that the build closes, and the manifest fields agreed in T2. Draft `routines/public-release.toml` for metasmith here, not in the metasmith repo (R35).

Gotchas: do not edit `docs/redux/*.md`. The interview session owns them.

### T16. Debrief

Run the `debrief` skill. Send one line to the coordinator, job 8162a704 on mizar.

## Built differently from this plan

The code is on `feat/redux-routines` (commits `0e9a8840` and `a95afeb1`). It differs from the approach above in these points:

- The plugin lives at `awm/plugins/routine/`, not `awm/services/routine/`, because redux-core made `awm/plugins` a built-in discovery root. Its manifest is `awm-plugin.toml`, the name the plugin contract settled on, not `plugin.toml`.
- Deploy is a verb of the `routine` tool: `routine deploy`, `routine deploys` and `routine refresh`. Today's gateway gives each service one tool, and a second service would split the store that tickets and runs share.
- The merge step moves no ref. `dev` and `release` move together in the promote step, in one `update-ref` transaction that checks both old values. A batch that fails its check leaves `dev` untouched, so `dev` never holds a merge that failed.
- When `release` holds commits that `dev` lacks, the batch merges `release` first, so the promote stays a fast-forward of both refs.
- A batch whose refresh fails ends in its own state, `refresh-failed`. Its tickets count as landed, because `release` already holds them. The fixing session gets the failed node and the batch's branches.
- The node list and the canonical node live in the node's install config, `<AWM_DIR>/config.toml`, in a `[fleet]` table that the cloud project writes (fleet.md R140). `home` names home base, `nodes` gives the rollout order with home base first, and `[fleet.project.<name>]` overrides both for a project landed elsewhere. `deploy.toml` names no host and refuses a `[fleet]` table. A `deploy` called on a node other than the canonical one forwards there, so a session never needs to know where the engine runs.
- awm refreshes with the `queue` kind: a session on each node runs `awm deploy` and watches the services come back. A `restart` command would restart the routine plugin that runs the batch.
- The routine plugin declares no refresh hook of its own, for the same reason. It refreshes with awm.
- The card half of the R26 verdict is a stub. No board verb writes to a session's card yet, so the verdict reaches the submitter's journal only.

## Proposed area doc changes

Do not apply these from this lane. The interview session owns `docs/redux/*.md`.

routines.md, requirements:

- R26 and R27: name the verbs as built. `routine deploy` returns a ticket. `routine deploys <id>` reads a ticket or a batch. The six steps stay, with one change: promote moves `dev` and `release` together, and merge moves neither.
- R27: add the `refresh-failed` outcome. The release landed, a node did not refresh, and the fixer gets the node.
- R29: add that the engine refuses a ref that moved under it and reports it as a promote failure, never overwrites it.
- R31: add that the install config's `[fleet]` table names home base, the rollout order and each project's canonical node, and that a deploy forwards to the canonical node. A session calls `deploy` on its own node. Pushing the branch to the canonical repo stays the session's job, and the deploy refuses an unknown commit with "Push first".
- R32: name the `queue` refresh kind as the mechanism for awm's supervised restart.
- R33: point at the `refresh` key of `awm-plugin.toml` in the plugin contract. A `hot` refresh runs each changed plugin's hook, and falls back to the project hook for any change outside a plugin or in a plugin with no hook.
- Add one line naming `deploy.toml` at a project's repo root as the deploy config, with `awm/plugins/routine/awm/routine/deployconf.py` as the source of truth for its keys. Do not list the keys in the doc.

routines.md, gap from today. Once the plugin is live on every node, remove these lines, because the build closes them in code:

- **No serialization.** One worker per project drains a queue. Promote checks both old ref values.
- **No fleet route in code.** The refresh step walks the install config's `[fleet] nodes` in order and stops at the first failure.
- **No test selection.** `run-tests.sh --changed <base>` maps changed paths to dists.
- **Known failures are rediscovered.** `scripts/full-tests.sh` keeps a known-failures file and fails only on a new failure.
- **No batching.** Every ticket waiting when a batch starts joins it.
- **No refresh hook.** `deploy.toml [refresh]` and the plugin `refresh` key.

Rewrite these lines rather than remove them:

- **No smoke tier.** `--smoke` now selects the marker, but few tests carry it. The gateway's smoke tier took 136 s against the 60 s budget, until redux-core's `silent_core` fixture fix (see Callouts).
- **Three schedulers exist.** The routine registry and its systemd timers exist. `events`, `dvc` and `transcripts` still run their own schedulers.
- **The canonical repo is on altair.** Unchanged, and now a cutover prerequisite: the cloud project's install config is to name mizar as home base.
- **Public releases are sessions.** The draft routine below replaces the hand-run build. The upload pull request per release stays with a session.

install.md: no change. The `refresh` key is already in the plugin contract, as redux-core adopted it at `38dc0f95`.

## Draft: metasmith `routines/public-release.toml` (R28, R35)

It belongs in metasmith's repo on its `release` branch. Install it on altair from a worktree that has `release` checked out. It has no timer, so Tony or an agent triggers it with a call. The steps follow metasmith's `RELEASE_PROTOCOL.md` on `release` and run from the root of that worktree.

> [routine]
> name = "public-release"
> description = "Gate, build and publish the metasmith release checked out here."
>
> [trigger]
> call = true
>
> [[step]]
> name = "version"
> run = 'test "$(git show dev:src/metasmith/version.txt)" != "$(cat src/metasmith/version.txt)"'
> timeout = 30
>
> [[step]]
> name = "gate"
> run = 'PYTHONPATH="$PWD/src" mamba run -n msm python -m pytest tests/metasmith -m "not docker and not e2e_docker and not e2e_agentic and not nextflow and not remote_host and not requires_docker and not requires_apptainer and not requires_ssh_localhost and not requires_docker_dev_image"'
> timeout = 2700
>
> [[step]]
> name = "producers"
> run = "unset PYTHONPATH && mamba run -n msm ./dev/metasmith.sh -brc && mamba run -n msm ./dev/metasmith.sh -br && mamba run -n msm ./dev/metasmith.sh -be && mamba run -n msm_node ./dev/metasmith.sh --build-gui && mamba run -n msm ./dev/metasmith.sh --vendor-library"
> timeout = 5400
>
> [[step]]
> name = "artifacts"
> run = "unset PYTHONPATH && for f in -bp -bd -bs -bc; do mamba run -n msm ./dev/metasmith.sh $f || exit 1; done"
> timeout = 5400
>
> [[step]]
> name = "publish"
> run = "mamba run -n msm ./dev/metasmith.sh -ud && mamba run -n msm ./dev/metasmith.sh -uc"
> timeout = 1800
>
> [fixer]
> scope = "release"
> prompt = "Read RELEASE_PROTOCOL.md. Fix the failed step on a branch, land it on release, then trigger public-release again."

Each step is one routine step, so a failure names the stage, as R27 does for deploys. `version` refuses a release whose version file matches `dev`, because the protocol bumps the version on `release` only.

Gotchas:

- Two jobs stay with a session: collecting library fixes from the product branches before the gate, and the pull request to the upstream repo after the publish. The library check reads the commit content, so a routine step cannot decide it.
- The gate's perf tests assert wall clock and fail under a concurrent build. Run nothing else on altair during the gate.
- `-uc` needs a per-host anaconda token at `~/.config/binstar/`. The routine does not check for it. A missing token fails `publish`.
- A routine runs on whatever its install worktree has checked out. The install pins only the definition's commit.

## Callouts

- The gateway smoke tier took 136 s against the 60 s check budget, so any batch that touched the gateway would fail its check. Four 30 s fixture teardowns in `awm/gateway/tests/test_mcp_proxy_timeouts.py` caused it. redux-core fixed it on `feat/redux-core` (`ThreadingHTTPServer` with daemon threads, now 9 s). This branch does not merge `feat/redux-core`. The final sweep brings the two together.
- The real `[fleet]` table belongs to redux-build/fleet, in the cloud project's install config. This lane proposed home mizar and the order mizar, alcor, altair, capella, leaving out mira and lyra. Nobody verified that list against the nodes that run awm today. `AWM_INSTALL_CONFIG` overrides the config path, so redux-core can fold it into the contract or drop it.
- The three old schedulers (`events`, `dvc`, `transcripts`) move onto routines only after this service is live on every node (gap T11). This build does not move them.
- Restarts that spare tether sessions and ssh ControlMasters (T8, R32) depend on the harness and channels lanes. The `queue` refresh kind gives the supervising session a place to do it.
- The deploy runs one engine per project on the canonical node (R31). Until the canonical repo moves to mizar, a real deploy here would act on a mirror. Nothing tonight runs one.
