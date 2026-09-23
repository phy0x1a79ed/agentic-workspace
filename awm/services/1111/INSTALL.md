# Installing the `1111` service

## Purpose & Contents

This file explains how the `1111` service reaches processes it does not own
and how to provision a box for it. It covers the account split, the gateway
registrations, provisioning and the gallery viewer. How each piece works lives
in the code: `deploy/1111ctl.sh`, `deploy/provision.sh`, `awm/svc1111/` and
`view/`.

## The account split

The service supervises two processes, the webui and a gallery viewer. Both run
under a separate OS account, `u1111`, whose home directory is mode 700. The
gateway's user cannot read the checkout, the venv, the generated files or the
viewer's index. The content therefore stays out of filesystem sweeps run as the
gateway's user, out of the workspace git tree and out of the nightly backup.

`/etc/sudoers.d/awm-1111` is the only bridge. It lets the gateway's user run
`/home/u1111/bin/1111ctl.sh` as `u1111` with eight fixed arguments and no
wildcard. `control.py` is the only caller. It always passes `sudo -n`, so a
missing rule fails at once instead of hanging on a password prompt.

## Gateway registrations

The service's RPC surface is lifecycle only. Browser traffic reaches each
process through a `kind=url` registration that `register.py` holds from a
supervised task: `/1111` on port 17860 and `/1111-view` on port 17861. Both set
`strip_prefix`, because both upstreams serve at their own root.

Neither prefix has a `kind=page` registration, so neither appears in `/ui/*` or
any page listing. Only `awm gateway list` shows them.

**CAUTION** `deploy/1111ctl.sh` repeats the ports from `control.py` and the
`/1111` prefix from `hub_adapter.py`. Change them together.

## Provisioning

1. Put the webui checkout at `/home/u1111/stable-diffusion-webui`, owned by
   `u1111`, with no git remote.
2. Run `sudo awm/services/1111/deploy/provision.sh` from the release tree.
3. Run `awm services restart 1111`.

The script creates the account if it is missing and installs the control
script, the viewer code and the sudoers rule. It validates the rule with
`visudo -cf` before it replaces the live file.

**WARNING** Never install `deploy/awm-1111.sudoers` by hand. A sudoers syntax
error breaks `sudo` for the whole machine.

Rerun `provision.sh` after every change under `view/` or `deploy/`. The viewer
runs from a copy in `/home/u1111/view`, because `u1111` cannot read the awm
tree. The script restarts a running viewer so it picks up the new copy.

## The gallery viewer

`view/` is a small FastAPI app that runs from the webui's venv. It must not
import `awm` and must stay compatible with that venv's Python and packages.
It keeps its SQLite index, thumbnails and trash in `/home/u1111/view-data`.

A keeper loop in the service restarts the viewer within a minute if it dies.
`1111_view_stop` also stops the keeper until the next `1111_view_start` or a
service restart.

**CAUTION** The index keys each image by its path under `outputs/`. A file
moved or renamed outside the viewer loses its virtual-folder memberships.

The server injects `<base href="{X-Forwarded-Prefix}/">` into the page. That
makes every relative URL resolve with or without the trailing slash. The
webui itself has no such fix, so use `/1111/` with the slash.

## Install

```bash
bash awm/services/1111/install.sh
```
