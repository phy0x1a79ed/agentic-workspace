# zotero

The reference library, mirrored into the shared vault. One note per paper, its
citation fields as labels, Zotero's collections as the note tree. Once a paper
is a note, linking to it is an ordinary note link.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
source is a running application rather than its database, why every request
carries a `Host` header naming an address it was not sent to, why the PDFs come
off a filesystem instead of the endpoint that exists to serve them, why there is
a file in the middle of a three-step sync, and how the mirror finds the note it
writes into.

Zotero's own behaviour belongs to Zotero's documentation. Where the vault lives
and how it is served is in `awm/services/trilium/INSTALL.md`. This file covers
only the boundary between awm and Zotero.

## The contract

**Zotero is a desktop application, and that is the whole shape of this
service.** The library lives on one machine, the vault lives on another, and on
sirius those can never be the same machine. So the sync is three verbs with a
file between them:

```
awm zotero pull      # needs the node the Zotero desktop is on
awm zotero ship      # carries library.json to the node that holds the vault
awm zotero apply     # needs the node the vault is on
awm zotero sync      # the above, as this node's role allows — what the timer runs
```

**The role says what a node does with the mirror.** `full` reads the library
and writes the vault. `pull` reads the library and never writes a vault.
`apply` writes the vault from whatever bundle arrives and never looks for a
library. Shipping is orthogonal to the role, so one node can be `full` and
still ship. An unrecognised role fails the service at start-up, because a typo
that quietly turned an apply-only node back into a puller reads as a node that
simply never syncs.

Today altair is `full` with a ship destination, and sirius is `apply`.

**The bundle is the thing that travels.** `pull` writes `data/zotero/` in the
vault scope — `library.json` plus the stored files — and commits a DVC pin.
That makes the mirror ordinary workspace data, versioned by the commit that
versions it.

**CAUTION** The bundle does not reach another node by merging a branch. Two
hosts' vaults are separate repositories with unrelated histories, the vault
declares no DVC remote, and `data/.gitignore` excludes the chunk. `ship` is the
only route, and it is an `rsync` straight into the far node's own vault scope,
where that node's bundle reader already looks.

**Only `library.json` travels.** It is 1.4 MB against 92 MB of stored files.
The apply side already skips a file the bundle does not hold, so a node that
receives a shipped bundle builds a complete mirror without the PDFs, and needs
no setting to do it.

**CAUTION** `library.json` becomes a read-only hardlink into the shared DVC
cache once a pull is pinned. The writer renames a new file over it. A write in
place fails outright, and a write that succeeded would land inside the cached
object and corrupt it for every scope and every commit that pins it.

**The bundle is ordered, and the order is load-bearing.** Zotero answers in no
stable order, so two reads of an unchanged library produced two different files
and two different digests. The merge sorts collections and items by ref. The
digest is the apply cursor, and an order-sensitive one makes every pull look
like a changed library.

**A tick that has nothing to do costs one request per library.** Each library
carries a `Last-Modified-Version`, and `pull` reads only the libraries whose
version moved. The versions live *in the bundle* rather than in a service
database, so a node that receives the bundle knows what it holds, and a node
that loses its service state has not lost its place.

**An apply that has nothing to do costs one search.** Apply records the
bundle's digest on the root note as `#zoteroApplied`, after the removal pass, so
a pass that died half-way retries rather than declaring itself done. A pass
whose digest already matches stops at the search. Without it an apply-only node
re-walks every paper on every tick, which is thousands of round trips proving
nothing.

**CAUTION** A hand-edited mirror note is no longer repaired until the bundle
moves. That is a deliberate change from earlier behaviour. Pass `--force` to
re-apply anyway.

Run `--dry-run` before any risky apply. It reports the resolved root, how the
root was found, the bundle's digest against the applied one, and how many notes
it would create, delete and visit. It writes nothing.

**One sync at a time, enforced by a file lock.** Each pass reads what the vault
already holds before it writes, so two passes overlapping each read "nothing is
there yet" and both create every note: two concurrent runs left 216 doubled
papers. The lock is `data/.zotero-sync.lock` and is taken with `flock`, because
the two callers are two processes — the timer inside the service and `awm
zotero apply` on the console — and an in-process lock cannot see across that.
`sync` holds it across both halves rather than taking it twice. A pass that
finds doubles collapses them, keeping the oldest.

**The mirror only ever rewrites what the mirror wrote.** A note it created
carries `#zoteroKey`. A note a person writes inside the library carries none,
so every pass is blind to it — and an item that leaves Zotero is deleted only
if it carries a key. That is the whole of how a mirror and a person share one
subtree.

## Where the library lands

The mirror writes under the one note carrying `#zoteroLibrary`. Set that label
from the Trilium UI on the note you want the library under. Move the library by
moving the label.

| what the search finds | what apply does |
|---|---|
| one labelled note | writes under it |
| none, and creation is allowed | creates a note titled `Library` under `parent`, and labels it |
| none, and creation is off | refuses, naming the label to add |
| two or more | refuses, naming every note id |

**CAUTION** The mirror never rewrites the note you labelled. It does not call
the upsert that would replace that note's body, and it does not patch the label
value you typed. Only a note the mirror created itself is written that way.

Refusing on two labelled notes is not fastidiousness. The removal pass is
scoped to the resolved root, so a root that alternates between passes builds the
whole mirror under one note, builds it again under the other, and deletes the
first set. Every note gets a revision and nothing reports an error.

**CAUTION** Trilium's search excludes archived notes, and the archived flag is
inherited. The mirror asks for them explicitly. Without that, one archived
ancestor hides notes the mirror already owns, the next pass creates second
copies carrying the same key, and the pass that collapses doubles cannot see the
originals either.

Set `ZOTERO_MAY_CREATE_ROOT=0` on a vault other people use. Creating a library
at the top of somebody's tree is worse than refusing to find the note.

Nothing enumerates the root's children, so unrelated children are safe. One
exception: the shelf step ensures a note by title under the root, so a
pre-existing child titled exactly `My Library` is adopted as that library's
shelf.

## Reaching the library

Three facts, none of them guessable, and each one looks like a different fault:

**The `Host` header is checked.** Zotero's HTTP server refuses any request
whose `Host` is not localhost, as a defence against DNS rebinding, and answers
`400 Bad Request` with no explanation. Reaching it from anywhere but the
loopback interface means sending the address in the URL and `127.0.0.1` in the
header. Without that, a healthy Zotero looks like a broken one.

**The file endpoint hands back a path, not the bytes.** `/items/<key>/file`
answers `302` with a `file:///C:/…` location. The bytes come off the
filesystem, which on a Windows host reached through WSL means translating the
drive letter to its `/mnt/` mount. An endpoint that redirects to the local
filesystem is useful only to something already on that filesystem.

**Only some attachments have bytes.** An attachment records a filename whether
or not the file was ever downloaded. What exists is what is in `storage/<key>/`,
and the item list does not say which those are. An attachment with no bytes is
dropped from the bundle rather than recorded, because a bundle promising a file
it does not hold makes `apply` fail on data rather than on a mistake.

**WARNING** Never read `zotero.sqlite`. It is open and journalled the whole time
Zotero runs, so a copy taken from underneath it records a state that may never
have existed. The API answers from the running application, for the same reason
`trilium` snapshots through ETAPI rather than copying `document.db`.

## Group libraries are included, and that default is load-bearing

A Zotero account has a personal library and any number of shared group
libraries, each with its own version counter and its own key space. `pull`
reads all of them.

**CAUTION** This is not generosity. On the account this was built against, the
personal library holds 2 stored files and its one active group holds 50.
Mirroring `users/0` alone produces a bibliography with almost no papers
attached to it, and nothing anywhere says why. Set `ZOTERO_GROUPS=0` only if
that is what you want.

**A key is unique within a library, not between them.** Two libraries may both
hold an item `ABCD1234`. So an item is identified throughout as
`<library>/<key>`, the stored files are filed under the same compound name, and
each library gets its own subtree in the vault. Flattening onto the bare key
looks harmless and makes one paper overwrite another.

## The vault is reached over the trilium service

This service does not import `awm.trilium` and does not open its own ETAPI
connection. `trilium` supervises the Trilium child — it starts it, restarts it,
and holds it down across a restore — so a second writer would write into a
database being swapped out from under it.

Everything goes through that service's note verbs. `awm/zotero/vault.py` is the
whole surface: eight operations, named there so the sync can be tested against
a dictionary.

## Install

```
./install.sh
```

`awm/gateway/install.sh` runs this on every deploy. There is nothing to install
but the Python: the library is read with `curl` over `ssh`, and the vault is
reached over the gateway. No client library, no credential, no daemon.

## Configuration

Environment, read at start:

| variable | default | what |
|---|---|---|
| `ZOTERO_SSH_HOST` | `capella` | the node the desktop is on. Empty means this host. |
| `ZOTERO_ORIGIN` | `http://172.25.176.1:23119` | where the server answers **as that node sees it**. On WSL that is the Windows side, so the default gateway rather than loopback. |
| `ZOTERO_HOST_HEADER` | `127.0.0.1:23119` | what the server insists on seeing. Deliberately not derived from the origin. |
| `ZOTERO_DATA_DIR` | `/mnt/c/Users/phybe/Zotero` | the Zotero data directory as that node can read it. |
| `ZOTERO_LIBRARY` | `users/0` | the personal library's id. |
| `ZOTERO_GROUPS` | `1` | mirror the group libraries too. |
| `ZOTERO_TIMEOUT_S` | `60` | per-request budget for the `ssh`+`curl` hop. |
| `ZOTERO_LIBRARY_NOTE` | `Library` | title of the note the mirror creates when it may create one. |
| `ZOTERO_ROLE` | `full` | `full`, `pull` or `apply`. An unrecognised value fails the service at start-up. |
| `ZOTERO_SHIP_TO` | empty | `host:/path/to/vault/scope` on the node that holds the vault. Spelled in full: the scope directory is named per host. |
| `ZOTERO_MAY_CREATE_ROOT` | `1` | set `0` on a shared vault, where apply must refuse rather than create a library. |
| `ZOTERO_SYNC_INTERVAL_S` | `1200` | how often the timer looks. |
| `ZOTERO_SYNC_ENABLED` | `1` | set `0` to stop the timer. |

## Verify

```
awm services list | grep zotero
awm zotero status
awm zotero apply --dry-run
```

`status` answers four separate questions: what the bundle holds, what this node
is for, whether the desktop is reachable from here, and whether the bundle and
the desktop disagree. An unreachable library is reported, not raised — the
desktop is sometimes asleep, and that is an answer rather than a fault.

An apply-only node never probes the library. `status` is the one verb here that
is not operator-gated, and on a node that cannot resolve the library's host the
probe is eight seconds of nothing on every call. A `status` that takes eight
seconds on such a node means the role is not what you think it is.

## Removing the mirror

Delete the shelf notes under the labelled root. Every mirror note's only branch
is a shelf, or a collection under one, so deleting the shelves removes the whole
mirror. The labelled note and its own children stay.

**CAUTION** There is one shelf per library that contributes items, not one per
library the account has. On this account that is two, not three. Deleting one
shelf leaves half the mirror behind.

Do not read the attachment count as a health signal. 46 items reference a
stored file, and those bytes never leave the pulling node, so the count is zero
on any node that received a shipped bundle.
