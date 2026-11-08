# The `dvc` plugin

## Purpose & Contents

This file covers what the `dvc` binary runs, the state it keeps, the setup each node needs, and how to check it. The deploy places the static binary at `<AWM_DIR>/bin/dvc`. It needs no install step. The binary serves the `dvc` service, the backup job runner (`dvc run-job`), the data layer's git hooks and merge driver (`dvc hook`, `dvc merge-driver`, `dvc status`, `dvc checkout`), the Globus login (`dvc globus-login`) and the pre-redux state check (`dvc state`).

## State

| Path | Holds |
|---|---|
| `<AWM_DIR>/services/dvc/dvc.db` | the two jobs' schedule rows (`dvc_jobs`) and their run history (`dvc_runs`) |
| `<AWM_DIR>/services/dvc/globus-tokens.json` | the Globus refresh token, access token and client, mode 0600 |
| `<AWM_DIR>/dvc.toml` | this node's chinook endpoints and prefix |

`state.toml` declares this state for `dvc state`.

**CAUTION** The single-flight guard is the partial unique index `dvc_runs_single_flight`. SQLite matches it by its expression text, so the status list in `src/runs.rs` must equal the index's.

## Setup on a node

1. Write `<AWM_DIR>/dvc.toml` with a `[chinook]` table: `local_endpoint` (this node's Globus endpoint), `remote_endpoint` (the chinook collection) and `prefix` (this node's tree on chinook). No value has a default.
2. Give the plugin a Globus login. On first start it imports the globus CLI's login, read-only, from `~/.globus/cli/storage.db`. Where the CLI never logged in, run `dvc globus-login` and paste the code it asks for.

**CAUTION** `prefix` is the path in both directions. `sync` writes the cache there, and `pull` reads objects back from it. A test prefix must be a sibling of the real one, never a child: nothing prunes chinook.

## The two jobs

`cache_sync` pushes `data/.dvc_cache` and never deletes on the remote. That append-only property keeps a local gc recoverable. `workspace_backup` mirrors the rest of the workspace under `<prefix>/workspace/`, and it deletes there what is gone here.

**WARNING** The mirror excludes every DVC checkout, because the archive carries those bytes. Disabling `cache_sync` while `workspace_backup` runs loses that data. `awm dvc coverage` shows what nothing covers.

routine's timers fire the jobs. On each start, and on each `dvc schedule`, the service installs the definitions in `routines/` and sets each enabled job's timer from its `dvc_jobs` row. The timer takes the cron as OnCalendar, with a 60 s jitter, the row's catch-up window, and the row's last fire as the slot already served. `dvc run-job --job <job>` blocks until the task ends. It is also the way to run a backup when the gateway is down. A run past its timeout stays live, and the service's watcher takes it to a verdict.

## Verify

1. Run `awm dvc jobs`. Each enabled job shows `registered: true` and a `next_due`.
2. Run `awm dvc runs --limit 5`. The last runs show their Globus verdicts.
3. Run `AWM_DIR=<AWM_DIR> <AWM_DIR>/bin/dvc state check`. It exits 0 when no pre-redux state was lost.
