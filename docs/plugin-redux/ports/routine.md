# Port plan: routine (absorbs events)

## Purpose & Contents

This file holds the approved port plan for routine (absorbs events), from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.**
- `awm/plugins/routine/`: 2089 src, 1393 test. The modules are hub_adapter, runner, store, definitions, timers, proc, escalate, outbound, land 625, fleetconf, deployconf and artifacts.
- `awm/services/events/`: 471 src, 361 test.
- The line in `scripts/land.sh` that runs `mamba run -n awm python -m awm.routine.land`.

**Tool surface.** Tool `routine`, major 1, tier on. Every verb uses the default surfaces.

| Verb | Params | Purpose |
|---|---|---|
| install | project*, path* | Store a definition TOML pinned at HEAD. Refuse a dirty file. |
| uninstall | project*, name* | Remove a routine and its timer. Refused while a run is live. |
| list | project | Installed routines with their timer, commit and last run. |
| trigger | project*, name*, wait:boolean, trigger (`call` or `timer`) | Start a run. Refused while a run is live. |
| cancel | project*, name* | Kill the live run's process group. |
| schedule | project*, name*, timer, jitter_s:integer, catchup_s:integer | Write and enable the systemd user units. Validate the expression with `systemd-analyze calendar`. |
| unschedule | project*, name* | Disable and delete the units. |
| runs | project, name, id, limit:integer | Runs, newest first. |

The land queue stays a CLI: `scripts/land.sh {submit,wait,batch,hold,bounce,status}`, with the same flags, the same JSON output and exit codes 0, 1 and 3.

**Changes to the surface.**
1. Remove the tool `events` (schedule, unschedule, list), its topics `schedule.tick` and `agent.wake`, and its funnel. Nothing subscribes to or calls any of them. grep across awm/, core/, scripts/ and the game-bot worktrees, the 60-day usage and both nodes' tables are all empty. Its only input is rlm-factorio, which is not core and is disabled.
2. Remove the verb `routine deploy`, a stub that only points at ci/land.
3. A run's `caller` field records `as_`, then the R9 session id, then the pid (R47).
4. A plugin shutdown kills each live step's process group and marks the run failed.
5. `schedule` gains two optional params, for dvc's jobs (T7):
   - `jitter_s` sets `RandomizedDelaySec`. Without it the unit keeps today's `RandomizedDelaySec=30`.
   - `catchup_s` bounds the catch-up. The unit gets `Persistent=true`. routine stores each timer's next slot in a new table `timer_slots(project, name, next_slot)`, created with `IF NOT EXISTS`. A timer fire later than `catchup_s` after its slot runs nothing, and its run row records the new state `skipped`. Without `catchup_s` the unit renders as today.

**State.** All of it is read in place, with no format change:
- `<AWM_DIR>/services/routine/routine.db`: `routines`, `runs`, the partial index `one_live_run`. The leftover `tickets` and `batches` tables stay untouched.
- The systemd user units `awm-routine-*`.
- `<AWM_DIR>/land/<project>/`: tickets, `batch.json`, `lease.json`, `lock`, `batches/`, `logs/` and `tree`. The JSON stays byte-compatible, with UTC ISO timestamps at seconds precision and 12-hex ids, because a Python `wait` can still poll during the first Rust batch.
- The `[fleet]` table of `<AWM_DIR>/config.toml`, which `AWM_INSTALL_CONFIG` can override.
- `deploy.toml` and its pins, read through `git show`.
- `<AWM_DIR>/artifacts/`.
- `events.db` stays on disk, unread.
- The new `timer_slots` table is additive. Nothing else in the file changes.

**Dependencies.** `network = ["*.globus.org"]`, `platform = ["linux"]`, no `[[needs.tool]]`.
- Steps inherit routine's egress proxy. The network list names the hosts of the installed steps' traffic: `*.globus.org` for dvc's `run-job` step. A routine whose steps need another host adds that host to routine's manifest in its own change. routine never declares `*`.
- The old GitHub entries go. They served the retired in-plugin deploy, and the land CLI pushes from the merger's shell, outside any egress proxy.
- The reflection and social needs go. A failed run falls back from the fixer to the notice to a local log (R48), so neither is hard.
- Plain calls remain: `reflection start_other` for the agent or fixer, and `social send` for the notice. Five dead outbound wrappers are deleted.

**Consolidations.**
1. events is deleted rather than merged: it has no live concern.
2. The land CLI becomes the `routine land <verb>` subcommand of the same binary. `scripts/land.sh` execs it.
3. dvc's clock moves onto routine, as T7 describes.

**The dvc interface.**
- routine owns *when*: the systemd timer, the run record and the failure notice.
- dvc owns *what*. Each dvc job is a routine definition in `core/plugins/dvc/routines/` with one check step, `<dvc binary> run-job --job <name>`, and a timeout of 86400.
- The step runs dvc's job in-process, the Rust form of `awm-dvc-sync`. It blocks until the job ends. It exits 0 on success or skip and nonzero on failure. It shares dvc's run table and single-flight slot.
- The scheduled path needs the gateway: the systemd timer runs `awm routine trigger`, which goes through it. The gateway-free path is the escape hatch, `dvc run-job` run by hand from a shell.
- dvc schedules each job with `jitter_s = 60` and `catchup_s = 72000`, matching today's jitter and its 20 h catch-up window.

**SDK needs.**
- `operator_dm()` in the config module.
- A shutdown hook.
- Config readers that work with no `Service`, for the land CLI.
- A stable path to each core plugin binary, placed by write-then-rename.

**Test plan.**
1. Port `test_routines` (19), `test_adapter` (4), `test_config` (13) and `test_land` (11), against temp bare repos.
2. A cross-implementation JSON test: Python-written land state loads in Rust, and Rust-written state loads in Python.
3. A shutdown-kill test.
4. The repo-script tests of `test_repo_files` move to `core/plugins/routine/tests/repo_scripts.rs`.
5. Unit tests for `jitter_s` and `catchup_s`: the rendered units, a fire inside the window that runs, and a late fire that records `skipped`.
6. Live check on mizar and altair:
   - a describe diff, where only `deploy` is gone and `schedule` gains its two params;
   - a fixture routine through trigger, runs, list, schedule and unschedule, with the unit files diffed byte for byte;
   - a failing routine whose notice arrives;
   - a real `scripts/land.sh status`;
   - a real no-op batch.

**Landing.**
1. One commit: add `core/plugins/routine`, change `scripts/land.sh`, add the pin, and delete both Python folders and their run-tests rows.
2. The ci/land and ci/merge skill edits land with T6. They make each skill run `$AWM_WORKSPACE/scripts/land.sh`, the live copy.
3. Submit with `--restart routine,events`. T6 lands alone in its batch.
4. Check `awm services`, `awm plugins`, `routine list` and `runs`, then the live check of the test plan.
5. Fallback, while the `awm` env exists (until PLAN.md T8): run the Python `land.py` from a worktree pinned at the commit before the port, with `mamba run -n awm`. It reads and writes the same land state.

Gotchas:
- The batch that lands this port runs the old Python land.py, which is safe. The next `land.sh` needs the Rust binary already placed on mizar, so the binary pin must ship in the same batch.
- A branch cut before the port carries a `land.sh` that calls removed Python. The skill change to the live copy fixes this.
- `routine land batch` runs while mizar's rollout replaces its binary. That is safe only with rename placement.
- The deploy check runs no cargo tests. PLAN.md T2 must add the core crates to the check.
- `node-refresh.sh` still runs `python3` for its health check. That is outside this crate and is flagged for PLAN.md T8 and T9.
