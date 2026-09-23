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

## Provisioning the isolated account (one-time, by hand, as root)

This service assumes the `u1111` account, its home directory, and
`/home/u1111/bin/1111ctl.sh` already exist — it never creates them. On a new
box:

1. `useradd -m -s /bin/bash u1111`; `chmod 700 /home/u1111`.
2. Put the app checkout at `/home/u1111/stable-diffusion-webui` (owned
   `u1111:u1111`), with no git remote.
3. `install -o u1111 -g u1111 -m 700 awm/services/1111/deploy/1111ctl.sh
   /home/u1111/bin/1111ctl.sh` — the canonical control script lives at
   `deploy/1111ctl.sh` in this dist so its fixes (see below) survive a
   rebuild; it is not generated or templated by anything, just copied in.
4. `printf 'setuptools<70\n' > /home/u1111/run/pip-constraints.txt` (as
   `u1111`, after `mkdir -p ~/run`) — see the script's own comments for why.
5. One `/etc/sudoers.d/awm-1111` rule: `tony ALL=(u1111) NOPASSWD:
   /home/u1111/bin/1111ctl.sh start`, and the same for `stop`/`status`/
   `restart` — four exact lines, no wildcard.

`deploy/1111ctl.sh` also carries three fixes discovered getting this
particular checkout running, worth knowing if the upstream app changes out
from under them: its shell scripts had CRLF line endings from originating on
a Windows checkout (worked around by invoking `bash ./webui.sh` rather than
relying on its own shebang); a legacy `setup.py`-only dependency
(`openai/CLIP`) breaks under a fresh `setuptools` pulled into pip's isolated
build environment (worked around with a `PIP_CONSTRAINT`/
`PIP_BUILD_CONSTRAINT` pin); and the app's own `--subpath` flag (which the
script always passes, matching `register.py`'s `PREFIX`) is required for
Gradio's generated asset links to resolve correctly behind the gateway's
`/1111` prefix — without it the page loads but every JS/CSS asset 404s in
the browser, since Gradio emits them relative to a root the proxy doesn't
actually serve from.

## Install

Same shape as every other feature service:

```bash
bash awm/services/1111/install.sh
```
