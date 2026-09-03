# zotero

The reference library, mirrored into the shared vault. One note per paper, its
citation fields as labels, its PDF attached, Zotero's collections as the note
tree. Once a paper is a note, linking to it is an ordinary note link.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
source is a running application rather than its database, why every request
carries a `Host` header naming an address it was not sent to, why the PDFs come
off a filesystem instead of the endpoint that exists to serve them, and why
there is a file in the middle of a two-step sync.

Zotero's own behaviour belongs to Zotero's documentation. Where the vault lives
and how it is served is in `awm/services/trilium/INSTALL.md`. This file covers
only the boundary between awm and Zotero.

## The contract

**Zotero is a desktop application, and that is the whole shape of this
service.** The library lives on one machine, the vault lives on another, and on
sirius those can never be the same machine. So the sync is two verbs with a
file between them:

```
awm zotero pull      # needs the node the Zotero desktop is on
awm zotero apply     # needs the node the vault is on
awm zotero sync      # both, in order — what the timer runs
```

**The bundle is the thing that travels.** `pull` writes `data/zotero/` in the
vault scope — `library.json` plus the stored files — and commits it with a DVC
pin. That makes the mirror ordinary workspace data: versioned by the commit
that versions it, carried to another node by merging a branch. It also makes
the sync auditable, because `git log -p` on `library.json` says what changed in
the library and when. Zotero's own state is not diffable and the vault's is a
SQLite file.

**A tick that has nothing to do costs one request per library.** Each library
carries a `Last-Modified-Version`, and `pull` reads only the libraries whose
version moved. The versions live *in the bundle* rather than in a service
database, so a node that receives the bundle knows what it holds, and a node
that loses its service state has not lost its place.

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

WARNING: never read `zotero.sqlite`. It is open and journalled the whole time
Zotero runs, so a copy taken from underneath it records a state that may never
have existed. The API answers from the running application, for the same reason
`trilium` snapshots through ETAPI rather than copying `document.db`.

## Group libraries are included, and that default is load-bearing

A Zotero account has a personal library and any number of shared group
libraries, each with its own version counter and its own key space. `pull`
reads all of them.

CAUTION: this is not generosity. On the account this was built against, the
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
| `ZOTERO_LIBRARY_NOTE` | `Library` | title of the note the mirror lives under. |
| `ZOTERO_SYNC_INTERVAL_S` | `1200` | how often the timer looks. |
| `ZOTERO_SYNC_ENABLED` | `1` | set `0` on a node that cannot reach the library, where every tick would be a logged failure saying so. |

## Verify

```
awm services list | grep zotero
awm zotero status
awm zotero sync
```

`status` answers three separate questions: what the bundle holds, whether the
desktop is reachable from here, and whether the two disagree. An unreachable
library is reported, not raised — the desktop is sometimes asleep, and that is
an answer rather than a fault.
