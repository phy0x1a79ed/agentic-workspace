# Installing the `dvc` service (DVC cache sync against chinook)

A Python feature service in the `awm.dvc` namespace. It owns both directions of
the workspace's relationship with the **chinook** Globus collection:

- `mirror` — the daily full-workspace backup.
- `status` / `resolve` / `pull` / `push` — the hash-selective inverse, moving
  only the cache objects one scope actually pins.

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

`prefix` is load-bearing in **both** directions: `mirror` writes the workspace
there and `pull` reads objects back from exactly that tree. If they ever name
different places, a restore silently finds nothing rather than failing.

Env vars override the file — `AWM_DVC_LOCAL_ENDPOINT`, `AWM_DVC_REMOTE_ENDPOINT`,
`AWM_DVC_PREFIX`, `AWM_DVC_GLOBUS_BIN`. Use `AWM_DVC_PREFIX` to exercise a round
trip against a scratch path: the real prefix is mirrored with
`delete_destination_extra`, so test writes there are on a deletion timer.

## The mirror is disaster recovery, not an archive

`mirror` runs with `delete_destination_extra: true`. A file deleted locally is
deleted on chinook at the next run. It protects against losing the machine, not
against `rm`.

It excludes DVC cache-checkouts, which are hardlinks into `data/.dvc_cache` and
fully rebuildable via `dvc checkout` — Globus cannot preserve hardlinks, so
mirroring both inflates a ~188 GB workspace to ~665 GB on the wire. **That
exclusion is only safe because `data/.dvc_cache` itself is mirrored.** If the
cache is ever excluded too, the exclusions stop being recoverable and the backup
starts losing data silently. Keep the pairing intact.

## Scheduling the daily mirror

A user systemd unit runs it. `ExecStart` should invoke the CLI, not a script
path — the awm checkout it would point into is a deploy target that gets
`reset --hard`, and a script committed there does not survive:

    # ~/.config/systemd/user/agentic-workspace-backup.service
    ExecStart=/path/to/envs/awm/bin/awm dvc mirror --wait 86400

    systemctl --user enable --now agentic-workspace-backup.timer

`mirror` refuses to stack a second destructive mirror on one still in flight
(pass `force=true` to override), so a slow run overlapping the next timer tick is
safe rather than a race.

## Restoring a scope

    awm dvc status  --scope projects/fabfos/dev      # local only, no network
    awm dvc pull    --scope projects/fabfos/dev      # returns a task_id
    awm dvc task    --task-id <id> --wait 3600
    # phase 'manifests' means call pull again — the leaves are now knowable
    cd projects/fabfos/dev && dvc checkout            # materialize into the worktree

`pull` is safe to repeat: each call re-resolves and moves whatever is still
missing. `dvc checkout` is required, not optional — a pulled object is a fresh
inode that nothing has linked into the worktree yet.

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
    awm dvc mirror --dry-run | head                   # builds the document, submits nothing
