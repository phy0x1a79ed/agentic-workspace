# Port plan: dvc

## Purpose & Contents

This file holds the approved port plan for dvc, from task T7 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Port dvc onto awm-dvcdata and routine's clock

Verb schemas are unchanged. Changes:
1. The cron loop leaves dvc. `schedule` converts the cron expression to OnCalendar and calls `routine schedule` with `jitter_s = 60`. It passes each `dvc_jobs` row's own `catchup_window_s` as `catchup_s`, and seeds the timer slot from the row's `last_fire_at`, so a job neither fires twice nor misses its window. An `@every` form is refused.
2. `jobs` reports routine's view: `clock: "routine"`, `registered` and `next_due`.
3. `scope_status` drops `dvc_bin` and adds `engine: "native"`.
4. `awm-dvc-sync` becomes `dvc run-job`, with the same exit codes. It is both the routine step and the escape hatch by hand. The subcommands `hook`, `merge-driver` and `globus-login` run without the gateway.

Globus:
- A native client replaces the CLI: Transfer API v0.10 over reqwest.
- On first start, dvc imports the token, read-only, from the CLI's `storage.db` into `services/dvc/globus-tokens.json` (0600). It keeps the CLI's client id.
- On first start, dvc also installs its two routine definitions. It schedules only the jobs whose `dvc_jobs` row is enabled.

State: `services/dvc/dvc.db` and `<AWM_DIR>/dvc.toml`, read in place. The partial-index DDL is kept verbatim.

Dependencies: `network = ["*.globus.org"]` and no needs. A routine outage shows as `registered: false` and refuses nothing.

The mirror keeps backup.py's `CACHEDIR.TAG` skip exactly.

Tests:
- Port the 108 Python tests.
- Test the Globus client against a mock, and the cron conversion with each row's window.
- Run `build_items` over a tree with a tagged `.pre-rust/`, and check that it is excluded.
- Live check on mizar: `describe`, `status`, `scope_status`, gc dry-run over every project, `coverage`, and the mirror dry run, which must list every `.pre-rust/` as excluded.
- Live check on altair, the one node with a live job (13 runs in 7 days): `jobs` and `runs` before and after, and the first scheduled fire after the land, which must come once and on time.
- Live check on capella: `run dry_run`, with the transfer items diffed exactly, and `task` on a known task, with every field equal.

Post-land check:
- `pgrep globus` is empty.
- The hooks name the Rust binary.
- The `.awm-py.bak` copies exist.

Rollback: revert, and keep `dvc.db`. Put each hook back from its `.awm-py.bak`. Restore `dvc.db` only when `state check` proves damage.

Gotchas:
- The token import is the single point of failure for every byte-moving verb. Test it on capella first, and keep `globus-login` ready.
- `run-job` and the service share `dvc.db` under WAL.

