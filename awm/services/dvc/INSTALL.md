# Installing the `dvc` service (the chinook remote for the shared DVC cache)

A Python feature service in the `awm.dvc` namespace. It owns both directions of
the workspace's relationship with the **chinook** Globus collection:

- `sync` — the append-only push of `data/.dvc_cache`.
- `status` / `resolve` / `pull` / `push` — the hash-selective inverse, moving
  only the cache objects one scope actually pins.
- `coverage` — what the remotes between them do *not* hold.
- `jobs` / `runs` / `run` / `schedule` — the two nightly backups, which the
  service schedules and watches itself.

On the collapsed MCP surface these are verbs under one `dvc` domain tool
(`dvc(verb="status")`, `verb="pull"`, …); CLI and HTTP stay expanded as `dvc_*`
(`awm dvc pull`, `POST /invoke {name:"dvc_pull"}`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`), then writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` and `AWM_DVC_GLOBUS_BIN` so the gateway can respawn the service
under systemd's minimal PATH.

## Prerequisite: the `globus` CLI, logged in

The CLI lives in its **own** env (default `globus`, override `GLOBUS_ENV`) and is
invoked as a subprocess — it is not a dependency of the `awm` env. It must be
present *and authenticated*; the login is an interactive operator step this
service cannot perform:

    mamba run -n globus globus login
    mamba run -n globus globus whoami

Tokens are long-lived but not eternal. When they lapse, every byte-moving verb
starts failing with an auth error from the CLI — re-run `globus login`.

## Configuration — `$AWM_DIR/dvc.toml`

**Required; there is no default.** Every value is node-specific, and the same
service source is deployed to every node in the fleet — a baked-in endpoint UUID
would silently make one node write into another's backup tree.

    [chinook]
    local_endpoint  = "<this node's Globus Connect Personal endpoint UUID>"
    remote_endpoint = "<the chinook collection UUID>"
    prefix          = "/Workspace_backups/<you>/<this node>"
    globus_bin      = "/path/to/envs/globus/bin/globus"   # optional; else resolved

`prefix` is load-bearing in **both** directions: `sync` writes the cache there
and `pull` reads objects back from exactly that tree. If they ever name
different places, a restore silently finds nothing rather than failing.

Env vars override the file — `AWM_DVC_LOCAL_ENDPOINT`, `AWM_DVC_REMOTE_ENDPOINT`,
`AWM_DVC_PREFIX`, `AWM_DVC_GLOBUS_BIN`. Use `AWM_DVC_PREFIX` to exercise a round
trip against a scratch path, and make it a **sibling** of the real prefix, never
a child: nothing prunes chinook, so a scratch write under the live tree stays
there forever.

## Append-only is the whole design

`sync` runs with `delete_destination_extra: false` and never deletes on the
remote. Chinook is the remote for the cache the way GitHub is the remote for the
code: it accumulates, and history is what it holds.

That is not a nicety. The cache is shared by every project on the machine, so
`dvc gc` is the operation most likely to be wrong, and under a delete-propagating
mirror a wrong `gc` became permanent at the next nightly tick. Append-only is
what makes it recoverable. It is asserted in `tests/test_sync.py` rather than
merely written here, because the failure mode is invisible: a mirroring sync
succeeds and looks identical in every report.

The cost is that chinook accumulates every object ever pushed. A remote prune,
run against the union of every project's history, is future work.

## Two backup paths

Two jobs run nightly, and they are deliberately different.

`cache_sync` pushes `data/.dvc_cache` and never deletes — the archive above.
`workspace_backup` mirrors everything *else* in the workspace to a sibling root
`<prefix>/workspace/`, and it **does** delete: a file removed locally is removed
there on the next run. It protects you from losing the machine, not from `rm`.

The mirror excludes every DVC-tracked checkout, because those are hardlinks into
the cache and Globus cannot preserve hardlinks — mirroring both would upload the
same bytes twice. **That exclusion is only recoverable while `cache_sync` also
runs.** Disabling the archive and keeping the mirror is not a saving; it is data
loss with no error message. Nothing enforces the pairing:

    awm dvc coverage            # per scope: uncommitted, unpushed, unpinned

Every destination the mirror emits is under `workspace/`, so the transfer that
deletes structurally cannot reach the archive beside it, whatever the exclusion
logic gets wrong. `tests/test_backup.py` asserts that rather than trusting it.

Symlinks are skipped, not followed and not recreated — following `.awm/data`
would drag the excluded checkouts back in, and *naming* a link to a directory
wedges the whole transfer (see `partition`'s docstring). Their targets are
backed up on their own account; a restore just does not get the links back.

Not covered by either: nothing, now, except what neither job can see — a branch
that exists on no remote is backed up as bytes but is still not *pushed*.

### What a healthy mirror looks like

The first full run moved **270.8 GB / 92 828 files in 6 hours**; later runs are
incremental. Two things about that will look like failure and are not:

- **A few hundred `FILE_NOT_FOUND` skips is normal.** A six-hour scan of a live
  workspace always races something being deleted. They land in the run's `note`
  and log at WARNING. The signature that matters is skips with *nothing*
  transferred — that logs ERROR, and it means the source was unreadable, not
  that the backup was empty.
- **`files_transferred` flatlines for long stretches** near the end while the
  transfer is busy with directory operations and skips. `globus task show`'s
  `Subtasks Succeeded` is the counter that keeps moving; judging progress by
  files reads as a stall twice over.

## Scheduling

The schedule lives in the service. It ticks inside the `dvc` process, so both
jobs are started, watched to a verdict, and recorded in one place:

    awm dvc jobs                          # cron, enabled, next due, last outcome
    awm dvc runs --limit 10               # history: task id, status, counters
    awm dvc run --job workspace_backup    # trigger one now, out of schedule
    awm dvc schedule --job cache_sync --cron '0 4 * * *'
    awm dvc schedule --job workspace_backup --enabled false

Defaults are `0 4 * * *` for the cache archive and `30 5 * * *` for the mirror;
a schedule you change is state and survives restarts. Specs are 5-field cron in
local time, or `@every <n><s|m|h>` — the interval form is what makes a
five-minute rehearsal possible without waiting for 04:00.

`jobs` also reports the loop's own health. `stopped: true`, or a large
`last_tick_age_s`, means nothing is firing — which is exactly how a backup
silently stops happening, so check that before concluding all is well.

A run declines rather than stacking a second whole-tree scan on one still in
flight; the refusal is a `skipped` row with the task it stood down for, not
silence. `--force` overrides it, and does *not* cancel the running transfer.

Progress is on the `job.status` topic. Events sent while nobody is subscribed
are lost, so read `runs` first and then tail.

### The escape hatch, and what moving the schedule cost

A systemd timer ran whether or not awm was healthy. An in-process loop does not:
**no gateway means no backup.** That is a real widening of the failure surface.
Three things mitigate it — a slot missed while the machine was down is caught up
on the next start (inside a 20-hour window, so a week powered off does not fire
a stale backup); the gateway is itself systemd-managed; and this still works
with the gateway down:

    /path/to/envs/awm/bin/awm-dvc-sync                    # the cache archive
    /path/to/envs/awm/bin/awm-dvc-sync --job workspace_backup

`install.sh` puts that console script in the env's `bin/` — not a path into the
awm checkout, which is a deploy target that gets `reset --hard` and has already
eaten one backup script whole. It submits, blocks to a terminal state, and exits
non-zero if the transfer failed. It shares the run table with the service, so it
takes the same single-flight slot and lands in the same history; a run that
outlives `--timeout` is left live on purpose for the service to adopt.

**Do not schedule `awm dvc sync` instead.** The service verb returns a task id
without waiting (correct for an agent, useless for a scheduled job: the unit
would report success on a failed backup), and the generated service CLI
dispatches through `/invoke` with a hard 600 s client ceiling.

`agentic-workspace-backup.timer` is **disabled**, not deleted, and so is the
`.service` unit it drove — a oneshot with no timer never fires on its own, and
between them they are one `systemctl --user enable --now` away from taking the
nightly archive back if the in-process scheduler ever has to be backed out. The
console script was called `awm-dvc-mirror` before the mirror was first retired;
`install.sh` removes that name so a unit still pointing at it fails loudly.

Nothing pages anyone. ERROR in `.awm/logs/services/dvc.log` is the only
notification channel there is.

## Restoring a scope

    awm dvc status  --scope projects/fabfos/dev      # local only, no network
    awm dvc pull    --scope projects/fabfos/dev      # returns a task_id
    awm dvc task    --task-id <id>                   # poll; add --wait <s> to block
    # phase 'manifests' means call pull again — the leaves are now knowable
    cd projects/fabfos/dev && dvc checkout            # materialize into the worktree

`--wait` on the CLI is bounded by that surface's 600 s `/invoke` ceiling
regardless of what you pass; over MCP it is bounded by the verb's own 3600 s
timeout. For anything longer, poll.

`pull` is safe to repeat: each call re-resolves and moves whatever is still
missing. `dvc checkout` is required, not optional — a pulled object is a fresh
inode that nothing has linked into the worktree yet.

**One unrecoverable manifest stalls the whole scope.** The two phases are
ordered, not merely preferred: while *any* `.dir` manifest is unresolved, `pull`
fetches manifests and nothing else, because the leaves are not yet nameable. So
a pin whose manifest is absent both locally and on chinook — a dangling pin no
sync could have captured — pins the scope in phase `manifests` forever, and the
leaves it *could* restore never move. `status` shows this as a nonzero
`unresolved_manifests` that a completed `pull` does not clear. Drop the dead pin,
or source it from another node; there is no way to restore around it.

## Why chinook is not a DVC remote

DVC's remote contract is per-file `exists` / `get_file` / `put_file`. A Globus
task is asynchronous and batch: seconds to start, minutes to finish. At
per-object granularity that is unusable. With a shared cache the scopes don't
need a remote anyway — `dvc checkout` materializes from cache with none
configured. This service is the chinook interface; `dvc checkout` is the
materialization step.

## Verify

    awm services list                                 # dvc → running
    awm dvc status --scope projects/fabfos/dev
    awm dvc sync --dry-run                            # builds the document, submits nothing
    awm dvc run --job workspace_backup --dry-run      # ...and the mirror's
    awm dvc jobs                                      # both jobs, and a live scheduler
