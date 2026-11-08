# Installing the `scopes` plugin

A static Rust plugin. The build puts its binary at `<AWM_DIR>/bin/scopes`, and `run.sh` executes that binary. The binary needs no env, interpreter or package install.

## Run

Do not start the service by hand. The gateway finds this folder through `awm-plugin.toml` and starts `run.sh` with the SDK's env (`AWM_HUB_URL`, the service name, the node). Before it serves, the binary takes one snapshot of the state that `state.toml` declares. Run `scopes state --help` for the state check subcommands.

The binary needs `git` on `PATH`. A fresh project's GitHub remote and `create` with `fork_url` also need `gh` with a login. Without `gh`, a fresh project gets no remote, and a fork is refused.

## Data

DVC data is native: provisioning, the drift check and the hook wiring come from the `awm-dvcdata` crate. The service needs no `dvc` binary. The git hooks it installs call `<AWM_DIR>/bin/dvc`.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_DATA_DVC` | `1` | `0` provisions no project's data |

The cache at `<workspace>/data/.dvc_cache` and `<workspace>/projects/` must be on the same filesystem. A materialised file is a hardlink into that cache. Across a filesystem boundary the file becomes a full copy, so each scope stores its own data.

## Search

`search` ranks scopes by the briefs that the `journal` tool returns for `journal_query` with `tag: "brief"`. When the journal does not answer, `search` matches names only and says so in a `degraded` block. The service keeps no search index of its own.

## Create hook

A project can make some scopes with its own script. Put `awm-project.toml` at the root of its main checkout, the worktree of the bare repo's HEAD branch:

    [scope]
    create_hook = "scripts/new-seat.sh"
    create_hook_prefix = "seats/"

The hook must register the scope with `scope ensure`. `src/hook.rs` describes the contract.
