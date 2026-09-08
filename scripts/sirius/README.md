# Operating sirius

## Purpose & Contents

This file is the standard operating procedure for sirius, the origin behind
`https://nexus.tony-xy-liu.com`. It says which script to run for a given change,
and it records the facts about the host that no script states.

What belongs here: the deploy model, the ownership split, the gates that decide
whether a change takes effect, the checks that confirm one did, and the ways
back. What does not belong here: anything a script's own header already states,
and per-service install detail. Each script under `scripts/sirius/` documents
its own arguments and steps at the top of the file. Read that header before you
run it.

## The box

sirius is a DigitalOcean droplet running Ubuntu 24.04 on x86_64, with 2 vCPU and
3.8 GB of RAM. Reach it as `ssh sirius`, which lands as the dev user with
passwordless `sudo`. The box holds no GitHub credential and no node toolchain.
Its DNS records point at Cloudflare, so everything it serves is reachable from
the public internet.

Its memory is the binding constraint. `install-awm.sh` sets `AWM_SEARCH=0`
because the search extra pulls torch. Building the Trilium monorepo here is not
possible for the same reason.

## What runs where

`/opt/awm` is a plain git checkout with **no remotes** and
`receive.denyCurrentBranch=updateInstead`. A deploy is a push from a dev box
into that working tree. Nothing on sirius ever fetches.

**CAUTION** Do not add a remote to `/opt/awm` and do not pull there. The box has
no credential to fetch with, and a remote invites a future session to try.

Inside the checkout, `projects/`, `data/`, `tasks/`, `main/` and `.awm` are
symlinks into `/var/lib/awm`. That state belongs to the `awm` service account,
which the dev user cannot read directly. Reach it with a shell:

    ssh sirius "sudo -n -u awm bash -s" <<<'ls /var/lib/awm'

**CAUTION** `sudo -n -u awm <command>` runs exactly one command. A `||`, a
redirection or a glob written in that argument string is interpreted by the
calling user's shell instead, which silently applies it with the wrong identity.
Feed a script to `bash -s` as above.

The gateway runs under `awm.service` as the `awm` account, with
`AWM_WORKSPACE=/opt/awm`. Per-box settings and secrets live in `/etc/awm/env`,
which the unit reads. Put a host-shaped value there, never in the unit file.
systemd kills by control group, so restart the unit rather than signalling
processes.

**WARNING** `/etc/awm/env` holds plaintext service credentials. Do not echo it,
copy it off the box, or paste its contents into a transcript.

`etc/` in this directory holds the host configuration `provision.sh` installs:
the nginx vhost and shared proxy snippet, the systemd units, the sshd and
fail2ban hardening, and the `profile.d` fragment that pins `AWM_WORKSPACE`.
A deploy carries a change there onto the box as a file. Run `provision.sh` on
the box afterwards to install it.

## Choosing a script

| The change | What to run |
| --- | --- |
| awm code or a built page, already on `release` | `scripts/sirius/deploy.sh` |
| a feature branch, through the whole fleet | `scripts/promote.sh <branch> --to sirius` |
| a first boot | `provision.sh` on the box, as root |
| a change under `etc/` | `deploy.sh`, then `provision.sh` on the box |
| the Trilium fork bundle | `awm/services/trilium/ship-bundle.sh` |
| a new paper in Zotero | nothing. altair pulls and ships it, and the timer here applies it |
| a new person | `scripts/sirius/add-user.sh <name>` |

`deploy.sh` covers the routine case on its own. It decides whether the diff
touched an install file, and it calls `install-awm.sh` when it did. Do not run
`install-awm.sh` by hand for a normal deploy.

Run `deploy.sh` from a dev box, from a checkout whose HEAD is the branch being
deployed and whose pages are built. It refuses otherwise. Expect a few seconds
when it only restarts the unit, and around twelve minutes when an install file
changed and the environment is rebuilt.

`promote.sh` runs the full test suite and needs a feature branch. It is the
wrong tool for a documentation change or a hotfix already sitting on `release`.

**CAUTION** `PUBLIC_SERVICES` in `install-awm.sh` is an allow-list, and it
gates two things rather than one. It is handed to the gateway installer as
`AWM_SERVICES`, so a service missing from it is never pip-installed and never
gets the `.runtime-env` sidecar its `run.sh` needs. It is also the enabled set.
A deploy that carries a whole new service therefore reports success and changes
nothing about what the box serves, and enabling the service in `enabled.json`
afterwards finds nothing to start. Add the name to that list to turn it on.

`install-awm.sh` refuses a run that would turn a service **off**, and names the
services it would lose. A stale checkout doing that silently is the recorded
incident. Set `AWM_ALLOW_DISABLE=1` when turning them off is the intent.

## Trilium on sirius

sirius installs the published Trilium tarball, which needs no build toolchain.
It **serves** the fork bundle built elsewhere and shipped by
`awm/services/trilium/ship-bundle.sh`. Read
`awm/services/trilium/INSTALL.md` for the layout, the constraints that script
enforces, and the slice feature it enables.

The switch between the two is a single file. `entry_point()` tests the
filesystem for `<fork>/apps/server/dist/main.cjs`, so copying the bundle in
selects the fork and deleting that one file selects the tarball again.

**WARNING** Shipping a bundle replaces the binary serving a live vault. Run
`awm trilium snapshot` first.

## The bibliography on sirius

sirius runs the `zotero` service with `ZOTERO_ROLE=apply`. It cannot reach the
Zotero desktop, which sits behind the private overlay this box is not on. altair
reads the library, and ships `library.json` into this box's own vault scope. The
timer here writes it into the note carrying `#zoteroLibrary`. Read
`awm/services/zotero/INSTALL.md` for the roles and the label.

**CAUTION** `ZOTERO_MAY_CREATE_ROOT=0` in `/etc/awm/env`. With no note carrying
the label, apply refuses rather than building a bibliography at the top of a
vault other people use. An empty `#zoteroLibrary` search is the ordinary reason
the mirror stops updating.

## Verifying a deploy

    ssh sirius 'systemctl is-active awm'
    ssh sirius 'AWM_WORKSPACE=/opt/awm awm trilium status'
    curl -sS -o /dev/null -w '%{http_code}\n' https://nexus.tony-xy-liu.com/trilium/

`deploy.sh` already polls the site until it stops answering 502, and prints the
code it settled on.

**CAUTION** An unauthenticated request to `/trilium/` answers `401`. That is the
edge gating the vault, and it is the correct result. Treat anything else as the
failure.

**CAUTION** Pass `--compressed` to `curl` when you read a JSON endpoint here.
Without it the body arrives gzipped and fails to parse, which reads as a
malformed response rather than a missing flag.

## Rolling back

Move the branch to the previous commit, rebuild the pages, run `deploy.sh`
again. The remote working tree follows the branch, so nothing on the box needs
editing.

For Trilium, delete `apps/server/dist/main.cjs` from the fork directory. Serving
falls through to the tarball on the next restart. `ship-bundle.sh` keeps the
previous `dist` beside it as `dist.old` for a full restore.
