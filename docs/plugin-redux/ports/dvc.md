# Port plan: dvc

## Purpose & Contents

This file holds the approved port plan for dvc, from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.** `awm/services/dvc/`: 3336 src, 2071 test. The modules are hub_adapter 531, scheduler 467, runs, backup, jobs, coverage, transfer, cache (moves to awm-dvcdata), scopedata, nightly, globus, config and sync. Its runtime needs the `globus` CLI env and the `dvc` CLI env. Both go.

**Tool surface.** Tool `dvc`, major 1, tier on. The timeouts are kept per verb.

| Verb | Params | Surfaces | Purpose |
|---|---|---|---|
| status | scope* | all | Pin and object counts against the local cache |
| resolve | scope* | all | The exact hashes a scope depends on |
| pull | scope*, dry_run:boolean | all | Two-phase Globus pull of the missing objects |
| push | scope*, dry_run:boolean | all | Globus push. Never deletes. |
| sync | dry_run:boolean, force:boolean | all | Run job `cache_sync` now |
| coverage | all:boolean | all | At-risk work per worktree |
| task | task_id*, wait:integer | all | Globus task state, optionally waiting |
| jobs | none | all | The two backup jobs and the clock's health |
| runs | job, limit:integer, status, since | all | Run history |
| run | job*, force:boolean, dry_run:boolean | cli, http | Trigger a job out of schedule |
| schedule | job*, cron, enabled:boolean | cli, http | Change a job's schedule |
| scope_status | project*, scope* | all | A scope's data view |
| mount | project*, scope*, chunks:array | all | Write the mount list and re-provision |
| gc | projects:array*, exclude:array, accept_missing:array, dry_run:boolean, keep | all | Keep-set gc of the shared cache |

Peer callers keep exactly today's private-mark treatment. The `job.status` topic keeps its payload. A watcher loop stays in dvc: an adopt sweep every 30 s, Globus polls every 30 s, a stall alert at 26 h, give-up after 10 failures, and the orphan threshold at 3600 s.

**Changes to the surface.** The verb schemas do not change. Behaviour changes:
1. The cron loop and catch-up leave dvc (`scheduler.py`). routine's timers fire the jobs.
2. `schedule` converts the 5-field cron to an OnCalendar expression and calls `routine schedule` (with `jitter_s` and `catchup_s`) or `routine unschedule`. It keeps `dvc_jobs` in step. An `@every` form is refused with a clear error, because no job uses it.
3. The `scheduler` block of `jobs` reports routine's view: `clock: "routine"`, `registered`, and `next_due` from `routine list`. The local computation is the fallback.
4. `scope_status` drops `dvc_bin` and gains `engine: "native"`. The `drift`, `data_status` and `checkout_detail` texts come from the native engine. `data_status` keeps DVC's key set.
5. The `awm-dvc-sync` console script becomes `dvc run-job --job <j> [--timeout] [--force] [--dry-run]`, with the same exit codes. New non-verb subcommands: `dvc hook <name>` and `dvc merge-driver %O %A %B`, which git invokes, and `dvc globus-login`, which Tony runs by hand.
6. Catch-up and jitter move to routine's timer: `catchup_s = 72000` keeps the bounded 20 h window, and `jitter_s = 60` keeps today's jitter.

**State.**
- `services/dvc/dvc.db` is read in place. Its schema is unchanged, and its DDL ports verbatim, including the literal partial-index predicates.
- `dvc.toml` is read as is. `globus_bin` is ignored, with a note in the log.
- A new `services/dvc/globus-tokens.json` (0600). On first start dvc imports the transfer token, read-only, from the globus CLI's `~/.globus/cli/storage.db`. That keeps the CLI's native-app client id, so nobody logs in again.
- `last_sync.json` adoption is dropped. Check each node for the file before landing.
- On first start, dvc calls `routine install` for its two definitions. If the `dvc_jobs` row is enabled, it also calls `routine schedule` with the converted cron. Both jobs are disabled on mizar and capella today.

**Dependencies.**
- `network = ["*.globus.org"]` for the service's own REST calls. reqwest honours the proxy.
- `platform = ["linux"]`.
- No `[[needs.tool]]`. dvc serves scope_status, mount, gc and coverage without routine. A routine outage shows as `registered: false` in `jobs`, never as a refusal.
- Plain calls: `routine install`, `schedule`, `unschedule` and `list`.

**Globus.** A reqwest/rustls client against Transfer API v0.10 replaces the CLI subprocess. It covers `GET /submission_id`, `POST /transfer` with the same document, `GET /task/<id>` mapped to the same 11 fields, `GET /task/<id>/skipped_errors`, and a 15 s poll loop for `wait`. A token that expires within 60 s is refreshed through auth.globus.org and written atomically. `globus-login` runs the native-app PKCE flow.

**Consolidations.**
1. One clock: routine.
2. One native Globus client.
3. One pin and cache model, in awm-dvcdata.
4. One worktree enumeration, `awm_dvcdata::git::worktrees`.
5. The escape hatch and the scheduled step are one subcommand, `run-job`.
6. The data hooks become a subcommand.

**Rust crates added.** `md-5` (via the crate), `sha2` (PKCE) and, optionally, `walkdir`. Nothing pulls in openssl.

**SDK needs.**
- A per-verb `timeout` in the ready manifest.
- The raw `as_` stamp.
- The privacy-marks reader, `awm_trust::privacy`, which knowledge's T4 moves in. dvc lands after it, or reads the gateway's current module until then.
- The workspace layout readers.
- A stable core-plugin binary path (`core_plugin_bin`), which the hooks embed.
- Subcommands that run without `Service::from_env()`.
- A supervised background-task helper.

**Test plan.**
1. Port runs (14), jobs (14), watcher (10), adoption (6), backup (25), sync (6), coverage (9), scopedata (14), private (7) and data_dvc_service (3).
2. The scheduler tests reduce to the cron conversion and the `schedule` forwarding.
3. New tests: the Globus client against an axum mock (submission, the transfer bytes, the task map, skipped errors, refresh, the 0600 file), the routine registration calls, and the `run-job` exit codes.
4. Live check:
   - On mizar: a describe diff with no schema change, `status` and `resolve` on a stock scope, `scope_status`, `gc dry_run` over every wired project, `coverage all`, `jobs` and `runs`, and `sync dry_run`.
   - On capella: `run dry_run` for both jobs with transfer items diffed exactly, `pull dry_run`, and `task` on the known task `5dee3af4-…`, every field equal (REST against the CLI).
   - With Tony's agreement, one real `cache_sync` on a backup node.

**Landing.**
1. Prerequisites: awm-dvcdata and routine are live.
2. One commit adds `core/plugins/dvc` with its `routines/` and deletes `awm/services/dvc`. Its operator notes move into the module doc.
3. Restart `dvc`.
4. Check:
   - `jobs` shows `registered: true`;
   - `pgrep globus` stays empty;
   - stock's hooks name the Rust binary;
   - the `.awm-py.bak` copies exist;
   - gc dry-run equals the capture.

Gotchas:
- The token import is the single point of failure for byte-moving verbs. Test it on capella before landing, and keep `globus-login` ready.
- `run-job` and the service share `dvc.db`, so both use WAL with a busy timeout. Map the unique-index violation to "declined".
- Do not reformat the partial-index DDL, or SQLite stops using it as the guard.
- The `run-job` step runs in routine's environment. Its Globus traffic goes through routine's egress, which lists `*.globus.org`. It reads the token file from `AWM_DIR/services/dvc`, which belongs to the same user.
