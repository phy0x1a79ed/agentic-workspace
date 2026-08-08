# Installing the `dvc` service (the chinook remote for the shared DVC cache)

A Python feature service in the `awm.dvc` namespace. It owns both directions of
the workspace's relationship with the **chinook** Globus collection:

- `sync` — the daily append-only push of `data/.dvc_cache`.
- `status` / `resolve` / `pull` / `push` — the hash-selective inverse, moving
  only the cache objects one scope actually pins.
- `coverage` — what the remotes between them do *not* hold.

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

## What is not backed up

Only `data/.dvc_cache` travels. Code is covered by GitHub. Everything else in the
workspace — scratch directories, run outputs, `.awm/` service databases — is
covered by nothing, deliberately.

    awm dvc coverage            # per scope: uncommitted, unpushed, unpinned

That report is what makes the choice informed rather than silent, and it is the
inventory a future LRU eviction pass has to consult before deleting anything
local.

## Scheduling the daily sync

A user systemd unit runs it, via the `awm-dvc-sync` console script that
`install.sh` puts in the env's `bin/`. Not a path into the awm checkout — that
is a deploy target which gets `reset --hard`, and it has already eaten one
backup script whole:

    # ~/.config/systemd/user/agentic-workspace-backup.service
    [Service]
    Type=oneshot
    Environment=AWM_WORKSPACE=/path/to/agentic_workspace
    ExecStart=/path/to/envs/awm/bin/awm-dvc-sync

`awm-dvc-sync` submits, then blocks to a terminal state and exits non-zero if
the transfer did not succeed — which is the whole point of running it from a
timer. **Do not use `awm dvc sync` here.** The service verb returns a task id
without waiting (correct for an agent, useless for a scheduled job: the unit
would report success on a failed backup), and the generated service CLI
dispatches through `/invoke` with a hard 600 s client ceiling, so no amount of
waiting on that path can cover a multi-hour transfer.

`sync` declines to stack a second scan on one still in flight (`--force`
overrides), so a slow run overlapping the next timer tick is wasted work avoided
rather than a race — and the console script treats that refusal as success, so
it does not page anyone.

The console script was called `awm-dvc-mirror` before the mirror was retired;
`install.sh` removes that name so a unit still pointing at it fails loudly.

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
