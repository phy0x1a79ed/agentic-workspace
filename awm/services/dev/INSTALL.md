# Installing the `dev` service

A Python feature service in the `awm.dev` namespace, and the dev sandbox it
drives. It needs the `awm` conda env to contain its package plus the
`gatewayclient` component.

## Install

    bash install.sh

`install.sh` editable-installs `gatewayclient` and this service into the `awm`
env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## The sandbox

`sandbox/run.sh` runs a worktree's own Rust gateway (`awm gateway serve`) on the
worktree's port band, with `sandbox/.awm/` as its state dir. It writes
`sandbox/.awm/config.toml` on every start: the worktree's `core/plugins`,
`awm/services` and `awm/plugins` as core roots, and the worktree's dist roots as
each service's `PYTHONPATH`. It uses `{release,debug}/awm` under
`scripts/rust-build.sh --target-dir` when built, else the `awm` on PATH. Set
`AWM_BIN` to choose. Put overrides such as `AWM_PORT` in the
gitignored `sandbox/.env`. Every `.env` key also reaches the services through the
config's `[env]`, such as `DSH_FORK_DIR` for dsh.

Drive it directly with `bash sandbox/run.sh start|stop|restart|status|seed|reset|logs`,
or through the service: `awm dev start --worktree <absolute worktree path>`. The
service refuses a worktree outside the workspace's `projects/`.

The sandbox does not run the `dev` service itself (`DEV_EXCLUDE`), so a sandbox
never drives sandboxes. It holds no secrets file, so services that need a
secret stay down. Test a change against the sandbox port. There is no shadow of
prod: `awm reload` picks up a changed plugin on a running gateway.
