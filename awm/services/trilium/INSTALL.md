# trilium

One shared knowledge base — documents rather than bullet points, PDFs and
figures beside the notes that cite them, and a history you can go back to.
Upstream is TriliumNext/Trilium, forked into `projects/trilium`, run as a single
server on loopback and served by awm's edge at `/vault`.

Trilium is single-user per instance, and that is what this design wants: one
instance, one database, one knowledge base that everyone signed in works in
together. It is collaborative by being shared, not by being replicated.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
vault is a second upstream on an existing listener rather than a mount or a host
of its own, why it has no password, why there are three kinds of database copy
and only one of them is a restore path, and what a shared origin costs.

Trilium's own architecture belongs to `projects/trilium` and its upstream docs.
Where the vault's content lives is `projects/vault`. This file covers only the
boundary between awm and Trilium.

## The contract

**One vault, and an account is the whole of joining it.** There is no per-person
scope, port, subdomain or DNS record — `scripts/sirius/add-user.sh <name>` makes
an auth account and stops, and that account reaches the vault immediately. This
is the property to protect when changing anything here: the moment adding a
person needs a second act somewhere else, the design has regressed to what it
replaced.

**The vault owns the root-level path surface, and its shell lives at `/vault`.**
Trilium has no URL-base setting: it serves its application shell from `/`, and
every asset reference in that shell is *relative*, so the assets are requested at
the URL root whatever path the shell came from. `/api/*`, `/src/*`, `/assets/*`,
`/bootstrap` and the rest therefore belong to the vault in any arrangement. What
is left to choose is where the shell is, and putting it at `/vault` rather than
`/` is what lets a mesh node keep its landing page with no second listener and no
port of its own. The list is `awm/httpsfront/vault.py`, next to the public
allow-list and for the same reason: a change to what a browser can reach should
be a reviewed diff.

CAUTION: `/vault/` with a trailing slash must never serve the shell. Relative
references resolve against the document's *directory*, so from `/vault` they
become `/src/…` and are found, and from `/vault/` they become `/vault/src/…` and
are not. The page paints and then hangs half-built, which is a far worse failure
than a redirect. The edge answers that path with a 308.

**There is no Trilium password, and that is a consequence rather than a
shortcut.** Its own login existed to say *which person*, back when there was one
instance each. With one shared vault it says nothing at all, and the awm edge
already knows who signed in — so a second password would ask the same question
twice and answer it worse. The child runs with `noAuthentication`, and a fresh
vault is provisioned over loopback (`provision.py`) so nobody's first visit is a
setup wizard.

What that setting costs, exactly: **protected notes stop working.** They are
encrypted with the Trilium password, and there is not one.

**The invariant it rests on.** `noAuthentication` stands down *every* guard
Trilium has — the shell, the internal API, the whole of ETAPI, the setup wizard's
password gate, and the WebSocket's own check. What replaces them is not weaker
but earlier: the edge authenticates the session before forwarding a byte. That
holds only while the edge is the **only** route in, so it is enforced rather than
asserted, in three places:

- the child binds loopback, and `child_env` both sets `TRILIUM_HOST` and *removes*
  `TRILIUM_NETWORK_HOST` — upstream's `Network.host` defaults to `0.0.0.0` and
  `TRILIUM_HOST` only out-ranks it by an ordering upstream is free to change;
- no awm code binds that child anywhere else, and `tests/test_no_listener.py`
  greps the package for the two symbols that would bring the retired per-person
  TLS front back;
- `install-awm.sh` *removes* any leftover Trilium nginx vhost and the retired
  `TRILIUM_FRONTS` / `TRILIUM_DOMAIN` keys rather than merely not writing them.
  Nothing else on a provisioned box ever deletes either, and a stale vhost
  pointing straight at the loopback port would be a public, unauthenticated
  knowledge base.

`TRILIUM_EDGE_ONLY=0` is the one supported way to reach the vault by another
route, and it takes the password back with it. One knob, so nobody can set half
of this.

**What a shared origin costs, stated because it was chosen.** The vault is on the
same origin as the rest of awm, which the retired per-person subdomains were not.
Trilium renders note content and runs user-authored *frontend* scripts — the
setting we pass disables *backend* scripting only — so a malicious or imported
note becomes script execution on the awm origin, able to make credentialed
same-origin calls as whoever is reading it. `awm_session` is HttpOnly, so it
cannot be read; it can be used. A shared vault raises this rather than lowering
it, because one bad note reaches every reader.

That is accepted, not overlooked. The mitigations are the minimal forwarded path
list (`/etapi/`, `/custom/`, `/share/` and `/mcp` are deliberately not forwarded —
see `vault.NOT_FORWARDED`), the operator-only verb split below, and a tight
public allow-list. The only complete fix is a separate origin, and the escape
hatch if the trust assumption ever changes is **one** DNS record — a `vault.`
host bound to the same edge — not one per person.

**Read verbs are public; everything else is an operator's.** The vault is shared,
so `restore` discards everyone's work and `snapshot` and `export` each rebuild
the whole thing on a two-core box. `status`, `snapshots` and `url` are reachable
from a browser; `start`, `stop`, `restart`, `provision`, `logs`, `snapshot`,
`export` and `restore` are refused for any caller that arrived through an edge.

The discriminator needs no new credential, because the edge already supplies one:
`httpsfront` overwrites `X-Awm-As` on every request it forwards and never
forwards an empty one, so **an absent identity means the call did not cross an
edge** — it came from `/invoke` on loopback, which is the host's own CLI. That is
`_operator_only` in `hub_adapter.py`, and it is the enforcement. The public
allow-list is defence in depth, and could not be the enforcement: a mesh node's
edge runs no profile and never consults it.

CAUTION: this is deliberately *not* `userroot.wrap_handlers`. That answers
"whose store?", which a shared vault never asks, and under
`AWM_USER_ROOT_STRICT=1` it raises for exactly the caller we need to admit.

**The children are on `compute`'s PROTECTED list.** The child is spawned in its
own session, so the `awm-service` pattern does not cover it, and a long-lived
node process that is idle until someone types is exactly the shape of a reaper
victim. The entry matches the bundle path, because nothing on the command line is
called `trilium`. Changing how `server.py` spawns the child without changing that
pattern makes it reapable again, and nothing reports it.

**Backend scripting and the SQL console are switched off explicitly.** Both
default off on a server build. They are set anyway, because a `config.ini` in the
data directory can turn either on, and on a public host either is arbitrary code
execution.

**Why not a gateway `kind=url` mount.** The blocker dsh records: the gateway's
WebSocket bridge forwards no headers at all, and Trilium's client holds a socket
open for every change it renders. The edge route is the design, not a shortcut
around one. Don't re-derive this.

**No `Origin` rewrite, unlike dsh.** dsh needs one because its harness compares
`Origin` to `Host`. Trilium's CSRF protection is a `csrf-csrf` double-submit
cookie, which travels correctly through an unmodified proxy. Setting
`rewrite_origin` here would hide nothing and buy nothing.

**`trustedReverseProxy=loopback` is required, not cosmetic** — but not for the
reason an older version of this file gave. It makes express read
`X-Forwarded-For`, so Trilium's per-IP rate limiter on the shell sees the real
visitor instead of every visitor collapsed onto `127.0.0.1`. It does *not*
control the `Secure` flag on Trilium's session cookie: `session_parser.ts` uses a
literal `config.Network.https`. `loopback` rather than `true`, because the edge
always connects from there and a blanket trust would let a forged header past
anything that reads a client address.

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
behind `checkApiAuth` — which wants an express session, and this service opens
none. So the single-note restore stays where the reader already is: one click in
Trilium's own revisions dialog. What the verb restores is the whole database, and
it moves the vault it replaced into `live/superseded/<timestamp>/` rather than
deleting it.

WARNING: on a shared vault a restore discards *everyone's* work since the
snapshot, not one person's. That is why it is operator-only and why it needs
`--confirm`, and why the page does not offer it.

## Registrations

One, plus a page that appears on its own:

| kind | name | prefix / port | what |
|---|---|---|---|
| `service` | `trilium` | `/svc/trilium` | the verbs and the supervisor |
| — | (page) | `/ui/trilium` | the reception page, mounted where `dist/` exists |

**This service binds no listener at all.** The vault answers on loopback
`awm.config.VAULT_PORT` (12511), and `awm.httpsfront` proxies `/vault` to it —
so the port is defined in `awm.config` rather than here, because two processes
must agree on it and neither owns it. There is deliberately nothing in this
package that could bind a socket; see the invariant above.

The reception page reports the server, the database, the snapshots and the bundle
as separate states, because those are four different failures with four different
fixes. It reports and does not control: every verb that acts on the vault is
refused for a caller arriving through an edge.

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

`scripts/promote.sh` closes the same gap for a fleet promotion, and closes it
*unconditionally* rather than on a pathspec: the fork lives in a separate
repository, so no diff over the awm tree can see it move. The install is stamped
and costs seconds when nothing did.

`deploy.sh` makes the change live on this node and stops there. Pushing to
GitHub, to capella's bare and to mira is fleet promotion, it is node-shape-
specific, and a script that guesses at it ships something other than what was
promoted.

CAUTION: the merge commit is made in a throwaway worktree of the local bare,
never in the release checkout. That checkout is a deploy target that gets
`reset --hard`, so a commit authored there is discarded later with no warning.

## sirius is not wired differently

It used to be, and the whole of that difference is gone. nginx proxies `/`
wholesale to the awm edge, and the vault is a path on that edge, so the public
host serves it by the same route and the same code as a mesh node. There is no
`TRILIUM_FRONTS`, no `TRILIUM_DOMAIN`, no generated vhost and no DNS record.

The one thing that is host-shaped: `client_max_body_size` in
`scripts/sirius/etc/nginx/awm-proxy.conf` is 512m, because the vault is behind
that one location and Trilium uploads whole PDFs and imports whole vaults in a
single request. nginx generates the 413 itself, so the application never sees it
and the editor simply appears to break.

## Verify

```
awm services list | grep trilium
awm trilium status
awm trilium snapshots
```

`status` answers four separate questions — is the process up, did it bind, does
it have a database, and is there a pinned snapshot — plus which bundle is being
served and whether it matches the revision on disk. Asked through an edge it
answers the first four and omits the pids and paths.

The check that actually matters is not any of those: **open `/vault` in a
browser, signed in, and confirm it paints and stays live.** A curl returning 200
proves the shell was served; only a browser proves the WebSocket connected, and a
vault whose socket never connects looks perfectly healthy and silently stops
showing anyone else's edits.

An end-to-end check of the data verbs, on the host:

```
awm trilium snapshot --name before-upgrade
awm trilium export
git -C projects/vault/main log --oneline -2
```

Both commit in the vault's scope. `snapshot` adds a pinned database copy,
`export` replaces `notes/` and commits the markdown with the pin in one commit.
Neither is reachable from a browser — run them where you can ssh.

## AGPL-3.0

Trilium carries it. Serving a modified version over a network triggers the
source-offer obligation. Our fork is public on GitHub, which satisfies it. Keep
it that way.
