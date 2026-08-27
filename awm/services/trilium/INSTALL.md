# trilium

A knowledge base per person — documents rather than bullet points, PDFs and
figures beside the notes that cite them, and a history you can go back to.
Upstream is TriliumNext/Trilium, forked into `projects/trilium`, run as one
server per user and put on the ZeroTier mesh behind awm's edge session.

Trilium is single-user per instance. That is not a limitation this service works
around — it is the reason for the design. awm's edge session is one shared
password for the whole workspace, so two people behind it are the same person.
One server each, with one database and one login each, tells them apart.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why a
person's server is fronted rather than mounted, why there are three kinds of
database copy and only one of them is a restore path, and why the two hosts that
run this are wired differently.

Trilium's own architecture belongs to `projects/trilium` and its upstream docs.
Each scope's `.awm/context.md` there says which branch does what. Where a
person's content lives is in `projects/userdata/README.md`. This file covers only
the boundary between awm and Trilium.

## The contract

**A person exists because a scope exists.** `discovered_users()` lists the
directories under `projects/userdata/trilium/`. Creating one is the whole of
adding a person, and the supervision loop picks it up without a restart. There is
no roster to keep in step with the filesystem.

**Ports are allocated once and remembered.** Front `12501 + slot`, upstream
`12511 + slot`, with the slot recorded in `.awm/services/trilium/ports.json` and
never reused. Deriving the slot from a sorted position looks equivalent and is
not: adding a person whose name sorts early would renumber everyone after them
and move a URL somebody had bookmarked, with no error anywhere.

**The database is deliberately uninitialized.** A fresh instance serves a setup
page and waits. Do not set the password from a script: that password *is* the
per-user identity, and this service holds no password at all. The person sets it
on their first visit.

**What this service holds is an ETAPI token.** `trilium authorize` either takes a
token the person created under Options → ETAPI, or exchanges a password for one
over loopback and discards the password. The token is written 0600 under
`.awm/services/trilium/tokens/`, and the person can revoke it from that same
screen. Prefer the token: a password passed to a verb travels through the
gateway, and a token does not have to.

**Why not a gateway `kind=url` mount.** The same two blockers dsh records: the
gateway's url proxy forwards the full request path without stripping the mount
prefix, and its WebSocket bridge forwards no headers at all. Trilium's client
holds a WebSocket open for every change it renders, so the second is fatal on its
own. A dedicated `awm.httpsfront` listener per person is the design, not a
shortcut around one. Don't re-derive this.

**No `Origin` rewrite, unlike dsh.** dsh needs one because its harness compares
`Origin` to `Host`. Trilium's CSRF protection is a `csrf-csrf` double-submit
cookie, which travels correctly through an unmodified proxy. Setting
`rewrite_origin` here would hide nothing and buy nothing.

**`trustedReverseProxy=loopback` is required, not cosmetic.** httpsfront
terminates TLS and forwards `X-Forwarded-Proto: https`. Without the trust setting
express reports the request as plain HTTP and Trilium declines to mark its
session cookie `Secure`. `loopback` rather than `true`, because the front always
connects from 127.0.0.1 and a blanket trust would let a forged
`X-Forwarded-For` past anything that reads a client address.

**Backend scripting and the SQL console are switched off explicitly.** Both
default off on a server build. They are set anyway, because a `config.ini` in the
data directory can turn either on, and on a public host either is arbitrary code
execution.

**The children are on `compute`'s PROTECTED list.** Each is spawned in its own
session, so the `awm-service` pattern does not cover it, and a long-lived node
process that is idle until someone types is exactly the shape of a reaper victim.

CAUTION: the entry matches the bundle path, because nothing on the command line
is called `trilium`. Changing how `server.py` spawns the child without changing
that pattern makes the children reapable again, and nothing reports it.

## Three kinds of copy, and only one is a restore path

| where | what | pinned | overwritten |
|---|---|---|---|
| `live/backups/` | Trilium's own daily/weekly/monthly rotation | no | on a schedule |
| `data/backups/` | named snapshots `trilium snapshot` moved there | yes | never |
| `notes/` | the markdown export | as text | every export |

**The rolling backups cannot be the DVC chunk.** It is the tempting arrangement —
they are the only consistent database copies on disk, because Trilium writes them
under its sync mutex. `dvc add` replaces every file it pins with a read-only
hardlink into the shared cache, and Trilium rewrites `backup-daily.db` in place:
the write fails on permissions and the daily backup stops. So Trilium churns in
`live/backups/`, and only copies this service moved under a timestamped name
reach the chunk.

WARNING: never pin the live database. `document.db` and its write-ahead log are
one logical unit, so a pin taken while the server runs records a state that never
existed — and it looks healthy until someone restores it.

**The markdown export is a derived view.** Trilium stores markup as HTML, so the
export is a conversion and importing it back is lossy. It is there to be read,
diffed, searched and merged by a person. Recovery is a snapshot, never this.

**`restore` is whole-vault, and that is a limitation with a reason.** Putting one
note's revision back is `POST /api/revisions/{id}/restore`, on the internal API,
behind `checkApiAuth` — which wants an express session, which wants the person's
password. This service holds a token and no password, on purpose. So the
single-note restore stays where the person's own session already is: one click in
Trilium's revisions dialog. What the verb restores is the whole database, and it
moves the vault it replaced into `live/superseded/<timestamp>/` rather than
deleting it.

## Registrations

Two, from one process, plus one that appears on its own:

| kind | name | prefix / port | what |
|---|---|---|---|
| `service` | `trilium` | `/svc/trilium` | the verbs, the supervisor, the fronts |
| — | (TLS front, per user) | `0.0.0.0:12501 + slot` | that person's Trilium, behind `awm_session` |
| — | (page) | `/ui/trilium` | the reception page, mounted where `dist/` exists |

A front is not a gateway registration. It is a listener this process owns, the
same shape as `httpsfront`'s own, and it dies with the service. Each person's
server listens on loopback `12511 + slot`.

The reception page reports the server, the front, the snapshots and the ETAPI
token as separate states, because those are four different failures with four
different fixes. It offers Snapshot and Export and does not offer Restore:
replacing a whole vault is not a thing a page with a refresh timer should do
behind one click.

## Install

```
./install.sh
```

`awm/gateway/install.sh` runs this on every deploy. Every step is idempotent and
skips itself when already satisfied. Two paths, and which one runs is the whole
difference between a build node and a serving node:

- **Build the fork.** `projects/trilium/release` *is* the runnable server, so
  every line we change is tracked TypeScript on a branch rather than an edit to a
  build artifact. Stamped on the fork's HEAD, its dirty flag and its lockfile
  hash, and skipped when none of the three moved.
- **Download the published tarball** for the pinned tag. Upstream ships a Node
  runtime inside it, so this path needs no toolchain at all — which is what lets
  sirius install in a minute instead of building TypeScript on two vCPUs.

`TRILIUM_INSTALL_MODE` forces one; the default picks the build when a fork is
checked out. A missing fork is a warning and a clean exit, because the gateway
runs every service's install under `set -e` and a hard failure aborts the whole
deploy on a node that simply does not serve Trilium. `TRILIUM_REQUIRE_SERVER=1`
makes it fatal where one is expected.

**The fork is a project, not a dependency.**

```
./bootstrap-fork.sh          # once per node
```

CAUTION: `git clone --bare` turns every branch on the fork into a local head, and
upstream maintains `release/v0.102.2`. Git stores refs as paths, so that head and
a `release` branch cannot coexist and `scope create` fails on the collision. The
script deletes the colliding heads; both remotes still carry them.

**Install artifacts live beside the service, not in workspace state.** `server/`,
`node-bin` and the tarball stamp are gitignored files under
`awm/services/trilium/`. On sirius the install runs as the dev user while the
gateway runs as the application account that owns the state root, so anything
written at install time has to be on the install side of that line.

## Deploy

```
./deploy.sh                       # this node's gateway
scripts/sirius/deploy.sh release  # sirius
```

`deploy.sh` does three things `awm deploy` does not: it promotes the commits into
the tree the editable install resolves `awm` to, it runs this service's
`install.sh`, and it builds the page. The install matters because `awm deploy`
re-runs a service's install script only when the *set* of installed dists
changes — a rebuilt bundle never lands after the first deploy, the same trap that
leaves drawio serving a stale client patch.

It makes the change live on this node and stops there. Pushing to GitHub, to
capella's bare and to mira is fleet promotion, it is node-shape-specific, and a
script that guesses at it ships something other than what was promoted.

CAUTION: the merge commit is made in a throwaway worktree of the local bare,
never in the release checkout. That checkout is a deploy target that gets
`reset --hard`, so a commit authored there is discarded later with no warning.

## sirius is wired differently

Three settings, and each one has a reason the mesh nodes do not share:

- **`TRILIUM_FRONTS=0`.** nginx behind Cloudflare is the public edge there, and
  the firewall admits 80 and 443 alone. A listener in the 12501 band would bind a
  port nothing can reach and mint a certificate nothing would trust.
- **`TRILIUM_REQUIRE_SCOPE_GIT=0`.** The box holds no GitHub credential and its
  `projects/` is a symlink into a state directory owned by the application
  account, so a scope there is a plain directory. Snapshots and exports are
  written and never committed; their history lives on a node that can hold a
  checkout.
- **One subdomain per person**, generated by `scripts/sirius/trilium-nginx.sh`
  from `awm trilium users`. Never a path prefix: Trilium has no URL-base setting,
  so an SPA served under `/trilium/<user>/` asks for its own assets at `/` and
  paints a shell that never finishes loading.

WARNING: each `<user>.$TRILIUM_DOMAIN` needs a DNS record before it resolves. Set
`TRILIUM_DOMAIN` when the records exist; until then the servers answer on
loopback only and `install-awm.sh` writes no vhost.

## Verify

```
awm services list | grep trilium
awm trilium status
awm trilium url --user tony
awm trilium snapshots --user tony
```

`status` answers four separate questions per person — is the process up, did it
bind, is that person's front serving, and do they have a pinned snapshot — plus
which bundle is being served and whether it matches the revision on disk.

An end-to-end check of the data verbs, on a vault whose password is set:

```
awm trilium authorize --user tony --token <from Options → ETAPI>
awm trilium snapshot   --user tony --name before-upgrade
awm trilium export     --user tony
git -C projects/userdata/trilium/tony log --oneline -2
```

Both verbs commit in that person's scope. `snapshot` adds a pinned database copy,
`export` replaces `notes/` and commits the markdown with the pin in one commit.

## AGPL-3.0

Trilium carries it. Serving a modified version over a network triggers the
source-offer obligation. Our fork is public on GitHub, which satisfies it. Keep
it that way.
