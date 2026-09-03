# trilium

One shared knowledge base — documents rather than bullet points, PDFs and
figures beside the notes that cite them, and a history you can go back to.
Upstream is TriliumNext/Trilium, forked into `projects/trilium`, run as a single
server on loopback and served by awm's edge at `/trilium/`.

Trilium is single-user per instance, and that is what this design wants: one
instance, one database, one knowledge base that everyone signed in works in
together. It is collaborative by being shared, not by being replicated.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
vault is a second upstream on an existing listener rather than a mount or a host
of its own, why it has no password, why the verbs that write notes are refused to
anyone who arrives through the edge, why the kanban board is Trilium's rather
than this service's, why there are three kinds of database copy and only one of
them is a restore path, and what a shared origin costs.

Trilium's own architecture belongs to `projects/trilium` and its upstream docs.
The patches we carry to it are the exception, because nothing in that project
says why they are there. Where the vault's content lives is `projects/vault`.
This file covers only the boundary between awm and Trilium.

## The contract

**One vault, and an account is the whole of joining it.** There is no per-person
scope, port, subdomain or DNS record — `scripts/sirius/add-user.sh <name>` makes
an auth account and stops, and that account reaches the vault immediately. This
is the property to protect when changing anything here: the moment adding a
person needs a second act somewhere else, the design has regressed to what it
replaced.

**The vault is one prefix, `/trilium/`, and the trailing slash is load-bearing.**
Trilium has no URL-base setting: it serves its application shell from `/`, and
every reference in that shell is *relative* — `./src/index-*.js`, `favicon.ico`,
`manifest.webmanifest`, `./bootstrap`, the runtime's `assets/v<version>/` and
`api/`, and the WebSocket URI it builds from `location.pathname`. Relative
references resolve against the document's *directory*, so a shell at `/trilium/`
puts every one of them inside `/trilium/`, where the edge strips the prefix back
off. The mount is `awm/httpsfront/vault.py`, next to the public allow-list and
for the same reason: a change to what a browser can reach should be a reviewed
diff.

CAUTION: the shell must never be served at `/trilium` without the slash. Two
things break. Every relative reference resolves to the site root instead of the
mount, so the page paints and then hangs half-built. And Trilium's own hashchange
parser refuses any URL that does not contain the literal `/#root`, so the browser
back button changes the address and the application ignores it. The edge answers
the slash-less path with a 308.

The prefix is also what keeps the vault's surface and awm's disjoint by
construction. `/api/`, `/assets/`, `/src/`, `/bootstrap` and `/favicon.ico` at
the site root are awm's, not the vault's. Do not add a root-level path to
`vault.py` to make something work — a relative reference that escapes the mount
is a bug in the mount.

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
list (`/etapi/`, `/custom/`, `/share/` and `/mcp` are deliberately not forwarded, and
are matched against the path *inside* the mount — see `vault.NOT_FORWARDED`), the operator-only verb split below, and a tight
public allow-list. The only complete fix is a separate origin, and the escape
hatch if the trust assumption ever changes is **one** DNS record — a `vault.`
host bound to the same edge — not one per person.

**Read verbs are public; everything else is an operator's.** The vault is shared,
so `restore` discards everyone's work and `snapshot` and `export` each rebuild
the whole thing on a two-core box. `status`, `snapshots` and `url` are reachable
from a browser; `start`, `stop`, `restart`, `provision`, `logs`, `snapshot`,
`export`, `restore` and `note_upsert` are refused for any caller that arrived
through an edge. `tests/test_operator_only.py` holds the two lists and fails
until a newly added verb is put in one of them.

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

**The day-note launchers are moved off the launchbar on every start.** Trilium
ships a "Today" button and a calendar widget. Both call `getDayNote`, which
*creates* `Calendar / <year> / <month> / <date>` on first click and leaves it
there. In a personal vault that is a feature. In one everybody shares it is a
dated folder tree in everyone's note list because one person once opened a
calendar. `provision.hide_day_note_launchers` moves both to the available set,
plus the mobile bar's copy of the calendar.

CAUTION: move, never delete. Upstream recreates a launcher whose *note* is
missing, under the parent its definition names, so a delete comes back visible.
It does not recreate a branch: `checkHiddenSubtree` enforces branch placement
only for items marked `enforceBranches`, which no launcher is. Re-applying on
every start is deliberate — the launchbar lives in the database, so anyone can
put the button back, and the tree it creates is shared.

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

## The note API, and why reading it is an operator verb

The service can do to a note anything a person can: read it, create, update,
delete, move, clone, place it under several parents at once, set and clear a
label or relation, and attach a file. `awm trilium --help` lists them.

**Every one is operator-only, reads included.** The rest of the write verbs are
operator-only because the vault is shared and one person's button acts on
everyone's work. `note_get` is different and lands in the same place for a
different reason: the edge deliberately does not forward `/etapi/`, so that a
note in the vault cannot run script that walks the vault. An open read verb
would be that same surface wearing awm's name. Nothing is lost by keeping it on
the host — the person reading the vault already has all of it in front of them.

Three places where the ETAPI shape underneath is not the shape a caller expects,
absorbed here so that every caller does not meet them separately:

- **Attributes are not part of creating a note.** ETAPI's create-note whitelist
  takes no attributes, so `note_create` taking `labels` is two or three calls
  wearing one verb.
- **A note's parent is a branch, not a field.** `note_place` sets the whole
  parent set in one call, and always adds before it removes: a note's last
  branch takes the note with it, so unplacing first deletes what you are moving.
- **An attachment's bytes do not travel in its JSON.** The create body's
  `content` is validated as a string. The row is made empty and the bytes are
  PUT after it as `application/octet-stream`, which is what makes express hand
  the route a Buffer rather than a mangled string.

`note_update` reports what actually moved, not what it was handed: a field is
compared before it is written, and offering a title that is already right
reports nothing and writes nothing. A mirror running on a timer offers every
field on every pass, so the alternative is a revision on every note every tick.

An argument that is an object — `labels`, `relations` — is spelled as JSON,
because one catalog projects each verb onto MCP, HTTP and the CLI and the CLI
has no object type: `--labels '{"status": "To do"}'`.

CAUTION: `note_upsert` is keyed on `(parent, exact title)`. Using it where a
title can repeat is how one person's writing gets overwritten. Key on a label
instead, the way the Zotero mirror keys on `#zoteroKey`.

## The board

Trilium ships a board view, so a kanban board here is not something this service
draws. A board is a `book` note carrying `#viewType=board`. A card is any note
beneath it carrying the label the board groups by — `#status` unless
`#board:groupBy` says otherwise. A board is therefore notes and labels and
nothing else, which is why the note API already reaches all of it and this
service offers no board verb.

It offered three once. `board_ensure`, `card_upsert` and `board_cards` are gone,
and each is replaced by something a person could do by hand:

- **Make a board in the browser.** It is one of the collection types, two clicks
  from the note menu. A board awm minted carried the labels without the
  template, so a vault ended up holding two kinds of board that did not look
  alike.
- **Read a board with a search.** `note_search` for the grouping label,
  restricted to the board's subtree, returns exactly the cards. `attrs_get`
  says which column each one is in.
- **Place a card with a note and a label.** `note_create` or `note_update` for
  the card, then `attr_set` for the grouping label.

**awm never invents a column.** It sets the grouping label only to a value the
board's `label:<groupBy>` definition already offers, and refuses the write
otherwise. The board view resolves its columns from that definition first, then
from its saved `board.json` attachment, then from the values notes carry — so a
value awm invents becomes a column on the next render, with nobody having asked
for one. This is a rule for whoever writes the next caller, not a check in the
code. The code that could have enforced it was the board module, and removing it
was the point.

CAUTION: the board groups its subtree **flattened and recursively**, so a card
nested two levels down still appears on it. Placement inside the board matters
less than the label.

### The two patches we carry

The fork was pristine at `v0.105.0` until this. Both patches are upstream bugs
rather than local taste, so both belong upstream. Sending them is a decision
nobody has made yet.

- **A card says where it lives.** Grouping is recursive, so a board shows notes
  from every depth of its subtree, and every one of them rendered as a bare
  title. Two notes called "Outline" under different parents read as the same
  card. A card now renders the titles between it and the board, joined by `/`.
  The path is accumulated on the way down rather than climbed back up from the
  card, because a note cloned to two places under one board has one path per
  branch and only the walk knows which branch a card was reached by.
- **A column change sticks.** Deleting or renaming a column re-rendered the
  board while the definition write was still in flight. That render resolved its
  columns from the definition it had just replaced, put the column back, and
  persisted what it resolved. So an empty column could not be deleted at all,
  and a rename left both names standing. The definition now lands on the copy
  the board holds before the write goes to the server. A definition an ancestor
  owns, or a template shares with notes off this board, is not this board's to
  rewrite, and a column it names is refused with a message rather than
  half-changed.

CAUTION: the tarball install path serves upstream's build, which has neither
patch. A node that installs from the tarball shows bare card titles and loses a
column change on reload. Only a node that builds the fork carries them.

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
`awm.config.VAULT_PORT` (12511), and `awm.httpsfront` proxies `/trilium/` to it —
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

The check that actually matters is not any of those: **open `/trilium/` in a
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
