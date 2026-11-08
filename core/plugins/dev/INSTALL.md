# The `dev` plugin

## Purpose & Contents

This file covers what the `dev` plugin drives and how to run its sandbox. The plugin is one static binary, `dev`, which the deploy places at `<AWM_DIR>/bin/dev`. It needs no install step.

## The sandbox

`sandbox/run.sh` runs a worktree's own Rust gateway (`awm gateway serve`) on the worktree's port band, with `sandbox/.awm/` as its state dir. Each start rewrites `sandbox/.awm/config.toml` and links the worktree's built core plugin binaries into `sandbox/.awm/bin/`. A core plugin that is not built stays down.

1. Build the worktree: `scripts/rust-build.sh build`.
2. Run `bash core/plugins/dev/sandbox/run.sh start|stop|restart|status|seed|reset|logs`, or `awm dev start --worktree <absolute worktree path>`.
3. Point the CLI at it with `AWM_GATEWAY_URL=http://127.0.0.1:<port>`.

Put overrides such as `AWM_PORT` in the gitignored `sandbox/.env`. Every `.env` key also reaches the services through the config's `[env]`.

The service refuses a worktree outside the workspace's `projects/`. The sandbox does not run the `dev` service itself (`DEV_EXCLUDE`), so a sandbox never drives sandboxes. It holds no secrets file, so services that need a secret stay down.

**CAUTION** A sandbox started from the old path `awm/services/dev/sandbox/` keeps its pid file there, and the new script cannot see it. Stop it with the old script before the worktree merges this layout.
