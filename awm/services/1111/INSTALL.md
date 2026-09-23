# Installing the `1111` service

Lifecycle control (start/stop/status/restart) for a webui process that this
service does **not** own the filesystem for.

## Why this service looks different from every other feature service

Every other service in `awm/services/` runs its child process as the same
Linux user as the gateway itself, and owns its data directly (see
`awm/services/trilium/awm/trilium/server.py::Child` for the normal shape:
`subprocess.Popen` + `setsid` + `PR_SET_PDEATHSIG`, all as one user).

This one supervises a process that runs under a **separate, permission-locked
OS account** — its checkout, venv and generated content are not readable by
the account the gateway runs as. That split is deliberate: it keeps the
content out of an ordinary filesystem sweep run as the gateway's user, out of
the workspace's own git tree, and out of the workspace-wide nightly backup
(none of which walk outside the workspace root or across a UID boundary).

What makes supervision possible anyway is one thing: `/etc/sudoers.d/awm-1111`
grants the gateway's user `NOPASSWD` rights to run exactly one script —
`/home/u1111/bin/1111ctl.sh` — as the other account, with exactly four fixed
argument forms (`start`/`stop`/`status`/`restart`), no wildcard. `control.py`
in this package is the only code that invokes it, always via `sudo -n -u
u1111 <script> <verb>` — the `-n` flag means a missing or misconfigured
sudoers rule fails immediately instead of hanging on a password prompt this
service could never answer. This service has no other access to that
account's files; it cannot read the checkout, the venv, or anything the
webui generates.

## Exposing the running webui

Lifecycle is all this service's RPC surface does. The webui itself is a full
Gradio app — proxying its HTTP/WS traffic through the same request/response
verb surface as `start`/`stop` would be the wrong shape for it. Its traffic is
reached through the ordinary external `kind=url` registration mechanism
instead — the same one `awm gateway register --url ... --prefix /1111` uses
interactively. That command is a foreground lease-holder (POST `/hub/register`,
then hold a WS open until Ctrl-C), which doesn't survive a restart on its own,
so `register.py` does the identical POST-then-hold-lease dance from a
supervised background task started in `on_start` — the prefix comes back
automatically whenever this service does, no terminal left open anywhere.

Deliberately **no** `kind=page` registration anywhere — so `/1111` never
appears at `/ui/*`, in `/tools`, or in `awm services list`'s page column. Only
`awm gateway list` shows the registration exists.

## Install

Same shape as every other feature service:

```bash
bash awm/services/1111/install.sh
```
