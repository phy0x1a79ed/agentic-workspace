# zotero

The reference library, mirrored into the shared vault. One note per paper, its
citation fields as labels, Zotero's collections as the note tree. Once a paper
is a note, linking to it is an ordinary note link.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
library is read from Zotero's own service rather than from a machine, why the
personal library is called `users/0` when the service calls it something else,
why a saved paper cannot be read any sooner than it is, what a window read can
and cannot see, why every list inside a record is sorted, and how the mirror
finds the note it writes into.

Zotero's own behaviour belongs to Zotero's documentation. Where the vault lives
and how it is served is in `awm/services/trilium/INSTALL.md`. This file covers
only the boundary between awm and Zotero.

## The contract

**Zotero is already a sync system, and this is a reader of it.** Every desktop
signed into the account uploads about three seconds after an edit, and every
other signed-in client is pushed the change over a websocket and syncs at once.
So awm does not move the library between machines and does not decide which
machine to trust. It subscribes to the same stream a desktop does, reads the
same copy a desktop reads, and writes the vault.

```
awm zotero status     # what the mirror holds, and whether the stream is live
awm zotero sync       # read whatever moved and write it into the vault
awm zotero apply      # write the bundle into the vault, without reading
awm zotero pull       # read the library into the bundle, without writing
```

**One node runs this, and only one.** The vault is shared, so a second mirror
against it sees the first one's notes and works from its own older bundle. Each
pass then deletes what the other just wrote. Nothing reports an error and every
note gains a revision. Today that node is sirius.

**A pass reads only what moved. The bundle stays a full picture of the
library.** Those are one decision rather than two, and the second is what makes
the first safe. Absence from the bundle is how the apply retires a paper, so a
partial *read* is only ever folded into the whole library the bundle already
holds. Nothing downstream of the fold knows a read can be partial.

Reading whole is still in the code, and it is the only read whose absences mean
anything. It happens on the first pull, on `--force`, once a day per library, and
whenever a window cannot explain what changed. See *Reading only what moved*.

**One sync at a time, enforced by a file lock.** Each pass reads what the vault
already holds before it writes, so two passes overlapping each read "nothing is
there yet" and both create every note: two concurrent runs left 216 doubled
papers. The lock is `data/.zotero-sync.lock` and is taken with `flock`, because
the two callers are two processes — the service's own loops and `awm zotero
sync` on the console — and an in-process lock cannot see across that. A pass
that finds doubles collapses them, keeping the first.

**The mirror only ever rewrites what the mirror wrote.** A note it created
carries `#zoteroKey`. A note a person writes inside the library carries none, so
every pass is blind to it — and an item that leaves Zotero is deleted only if it
carries a key. That is the whole of how a mirror and a person share one subtree.

## Reading the library

**The key is read-only, and the code cannot write whatever the key allows.**
`ZOTERO_API_KEY` is a key from `zotero.org/settings/keys`. The reader issues
only GET and the method is not a parameter anywhere in `source.py`, so the day
something needs to write is a deliberate change rather than an accident. This
matters because the node holding the key is a public host and because a write
made with it propagates to every machine the account syncs. `status` reports
whether the key can write, so the capability cannot be quietly forgotten.

**CAUTION** The personal library is called `users/0` here, not the account's
number. The desktop's copy of Zotero's interface numbers the signed-in user `0`,
and every note the mirror has ever written carries `#zoteroKey=users/0/<key>`.
Letting the real number reach the bundle would change the identity of every
paper at once: the pass would find no note for any reference, build a second
copy of the whole library, and delete the first for being absent. `source.path_of`
is the one place the name and the address meet. A group is numbered the same by
both and needs none of this.

**A paper saved a moment ago cannot be read yet, on any machine.** An item Zotero
has not uploaded carries version `0`, and the version a library reports is the
one the service assigned, so a fresh save is in no `since` window anywhere. That
is why nothing here tries to read a desktop to get ahead of the service: the
paper is not there to read. The three seconds Zotero waits before uploading are
the floor on how fast this can be.

**Three facts about the two interfaces, because the service used to read the
other one.** A desktop's copy has no `deleted` route at all, so a mirror reading
a desktop could only learn that a paper was gone by re-reading everything — this
one has the route, which is what makes reading only what changed safe. A desktop
refuses any request whose `Host` header is not localhost, which made a healthy
Zotero look broken from anywhere else. And a desktop is a desktop: it is asleep
when the machine is off. None of the three applies now.

**Group libraries are included, and that default is load-bearing.** A Zotero
account has a personal library and any number of shared groups, each with its
own version counter and its own key space. On this account the personal library
holds two stored files and its one active group holds fifty, so mirroring
`users/0` alone produces a bibliography with almost no papers attached to it and
nothing anywhere saying why. Set `ZOTERO_GROUPS=0` only if that is what you want.

**A key is unique within a library, not between them.** Two libraries may both
hold an item `ABCD1234`. So an item is identified throughout as
`<library>/<key>`, and each library gets its own subtree in the vault.
Flattening onto the bare key looks harmless and makes one paper overwrite
another.

## Being told, rather than asking

The service holds one websocket to Zotero's event stream and is pushed a frame
naming a library and its new version whenever one changes. Between changes it
costs an idle socket.

**The failure to design against is not a crash.** It is a socket that stays open
while the subscription behind it is gone, which is indistinguishable from a
library nobody is touching. Keepalives only prove the far end is alive. There
are three guards, and the third is the one that matters:

- the websocket's own ping and pong, which catch a peer that stopped answering
- an idle deadline, which rebuilds a stream that has said nothing for an hour
- the periodic pass underneath, which is a floor rather than a mechanism

The third is why nothing above it has to be perfect. A stream that goes deaf
costs staleness until the next tick, not a stopped mirror. `status` reports how
long the stream has been silent, which topics it holds, and what refused it — a
subscription that reaches nothing otherwise waits for ever in perfect health.

Being told and doing the work are two loops on purpose. The stream must keep
reading its socket while a pass runs, or a change landing during a long apply is
never delivered. So the callback sets a flag and returns, and the worker clears
that flag *before* the pass rather than after: a change arriving mid-pass has to
leave it set, so the next turn picks it up instead of the pass swallowing it.

**The frame decides which libraries the pass reads, and names them.** A push
reads only what the stream named, and takes each library's display name from the
bundle rather than asking Zotero — that name is inside every fingerprint and is
the title of the library's shelf, so a missing one rewrites a library's worth of
notes and renames their shelf to nothing. Only a library this bundle has never
seen is worth a request for a name.

The names accumulate rather than replacing each other. The loop coalesces a burst
behind one flag, so a save to the personal library and one to a group inside the
settle window would otherwise leave whichever came first for the floor tick.

**A push that finds the lock held is retried, not dropped.** The pass holding it
may have read its libraries before this frame arrived, and the flag is cleared
before the pass, so dropping the frame left that paper for the floor tick twenty
minutes away.

**`ZOTERO_SETTLE_S` is half a second.** It collapses a burst into one pass, and a
second pass now costs one small request rather than a whole-library read, so
paying more than that on every wake is the wrong side of the trade.

## Reading only what moved

**The read is also the movement probe.** Asking a library what has changed since
a version returns nothing when it has not moved, and every answer names the
library's current version in its own header. So there is no request asking where
a library is: that number arrives with the records, or with their absence.

Three answers, and the middle one is the whole design.

- **Records came back.** They explain the version, and one of them is the paper
  somebody is waiting for. That is enough to write the vault. The collections
  window is read only when a record names a collection the bundle does not hold,
  and on a push the deletions window waits for the floor tick — a paper appearing
  quickly and a paper disappearing quickly are different requirements, and only
  the first has somebody standing over it.
- **Nothing came back and the version rose.** Something changed that a window
  cannot show. Nobody is waiting on it, so the pass looks properly: collections,
  deletions, and when those are empty too, the library whole.
- **Nothing came back and the version held.** Skip.

**CAUTION** The escalation needs all three to be empty. A collection rename and a
tag change each move the version and return no items, and escalating on those
would spend the whole read this exists to avoid.

**A paper in Zotero's trash is invisible to every route.** Not in `/items`, not
in a window, not in `deleted` — that route reports a permanent removal. The
library's version moving with nothing to show for it is the only sign there is,
which is what the escalation above is for, and a whole read once a day per
library is what bounds the case where a trashing shared a pass with some other
change.

**A whole read is only authoritative about absence while it is a snapshot.** The
walk pages by offset over a list the service orders by modification date, so an
item edited part-way through jumps to the front and pushes the item on the page
boundary out of the window. That item is then absent, and absence retires a
paper. Each page's version is compared against the first; a walk that moved is
restarted, and one that will not settle raises rather than returning a short
answer.

**Two refusals guard the same failure.** A library whose read raised is carried
unread and keeps every record the bundle holds for it — not read is not the same
as read and found empty. And a whole read that comes back empty against a bundle
that is not raises `ZoteroLostItsLibrary`, because that is the one shape that
empties the vault in a single pass with every call succeeding.

**A window read cannot merge into a bundle written before notes carried keys.**
Such a bundle holds a bare list of note HTML and nothing can say which entry an
arriving note replaces. It is read as holding no notes, and the whole read that a
bundle with no whole-read stamp is due rebuilds it. That is the upgrade path and
it needs nobody to run anything.

## What a pass costs

A note carries `#zoteroStamp`, a fingerprint of the whole bundle record behind a
render version. A paper whose fingerprint already matches is skipped without a
call. Not Zotero's item version: a version cursor cannot see a group renamed or
a file arriving after the item stopped changing, and a digest of the record is a
strict superset of every narrower cursor.

Measured on the live library of 823 papers in 20 collections:

| pass | requests to Zotero | seconds |
|---|---|---|
| a push, narrowed to the library that moved | 1 | 0.3 |
| a floor tick where nothing moved | one per library | 0.5 |
| catching up 60 versions across three libraries | a handful | 2.6 |
| a whole read of all three libraries | about sixteen | 22 |

**Every list inside a record is sorted, and that is correctness rather than
tidiness.** The fingerprint is taken over the whole record, so a list carrying
the service's answer order makes the same library state produce two different
records: two whole reads at one version once differed on 44 of 823 papers, and
the apply rewrote every one. It is also what lets a window read and a whole read
be compared at all, which is the property the whole design rests on.

**A paper's child notes are keyed by the note's own key.** A child note is its
own item with its own version, so adding one to a paper does not move that paper:
a window carries the note without its parent, or the parent without its notes.
Against a bare list neither can be merged, and the choice would have been between
losing notes and fetching a parent's children back over the network. Against keys
both are a dictionary update costing no request.

**CAUTION** `RENDER` in `sync.py` must be bumped whenever `_title`, `_card` or
`_labels` change what a note looks like. Forgetting is silent and total: every
note keeps a fingerprint claiming it is current, and only a forced pass notices.

**The fingerprint is written last**, after the placement. One written with the
content would mark a paper whose placement failed as current, and nothing
revisits it.

**Placement is checked on every pass whatever the fingerprint says.** A search
result already reports where a note is, so comparing is free — and it is what
puts back a paper somebody dragged in the interface, which no cursor would
notice. It is also why a renamed or moved collection needs no cursor of its own:
the papers keep the same parent note and none of them is touched.

**`--force` is the repair verb**, and the only thing that repairs a hand-edited
note or a fingerprint that lies.

**An apply that has nothing to do costs one search.** Apply records the bundle's
digest on the root note as `#zoteroApplied`, after the removal pass, so a pass
that died half-way retries rather than declaring itself done.

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

Refusing on two labelled notes is not fastidiousness. The removal pass is scoped
to the resolved root, so a root that alternates between passes builds the whole
mirror under one note, builds it again under the other, and deletes the first
set. Every note gets a revision and nothing reports an error.

**CAUTION** Trilium's search excludes archived notes, and the archived flag is
inherited. The mirror asks for them explicitly. Without that, one archived
ancestor hides notes the mirror already owns, the next pass creates second
copies carrying the same key, and the pass that collapses doubles cannot see the
originals either.

**CAUTION** A scan is capped, and a capped scan is worse than none: the pass
cannot see notes it owns, decides they are new, and creates a second copy of
each. Hitting the cap raises rather than proceeding.

Set `ZOTERO_MAY_CREATE_ROOT=0` on a vault other people use. Creating a library
at the top of somebody's tree is worse than refusing to find the note.

Nothing enumerates the root's children, so unrelated children are safe. One
exception: a shelf is found by its `#zoteroLibraryId` label, so a shelf note
renamed in the interface is renamed back rather than duplicated.

## Saying so in the vault

One note under the labelled root carries `#zoteroStatus` and records what the
mirror last did: when, from the stream or from the floor, the library versions,
the counts, and a link to the newest paper. The root points at it with
`#zoteroStatusNote`, which root resolution has already read, so finding it costs
nothing.

It is written only by a pass that changed something. A note rewritten on every
look would gain a revision every twenty minutes and say nothing.

It exists because a stalled mirror and an idle one are otherwise identical from
inside the vault. This one ran dead for days before anybody noticed.

## The vault is reached over the trilium service

This service does not import `awm.trilium` and does not open its own ETAPI
connection. `trilium` supervises the Trilium child — it starts it, restarts it,
and holds it down across a restore — so a second writer would write into a
database being swapped out from under it.

Everything goes through that service's note verbs. `awm/zotero/vault.py` is the
whole surface, named there so the sync can be tested against a dictionary.

## No files travel

The vault holds citations, not PDFs. Nothing here fetches or attaches a file,
and the pinned chunk is `library.json` alone.

That is a change: the mirror used to copy attachment bytes off the machine
holding the library, which needed a machine holding the library. Zotero's own
file storage can serve them, so putting PDFs in vault notes is possible in a way
it was not before. It is not built.

## Install

```
./install.sh
```

`awm/gateway/install.sh` runs this on every deploy. There is nothing to install
but the Python: the library is read over HTTPS from the standard library, and
the vault is reached over the gateway.

## Configuration

Environment, read at start:

| variable | default | what |
|---|---|---|
| `ZOTERO_API_KEY` | empty | a key from `zotero.org/settings/keys`. Required. Read access is all this needs. |
| `ZOTERO_USER` | discovered | the account's number. Read from the key when unset, which is one fewer thing to get wrong. |
| `ZOTERO_API_BASE` | `https://api.zotero.org` | overridable so a test can point elsewhere. |
| `ZOTERO_GROUPS` | `1` | mirror the group libraries too. |
| `ZOTERO_TIMEOUT_S` | `30` | per-request budget. |
| `ZOTERO_STREAM_ENABLED` | `1` | set `0` to fall back to the timer alone. |
| `ZOTERO_STREAM_URL` | `wss://stream.zotero.org/` | the event stream. |
| `ZOTERO_STREAM_IDLE_S` | `3600` | rebuild a stream that has said nothing for this long. |
| `ZOTERO_SETTLE_S` | `0.5` | wait this long after being told, so a burst becomes one pass. |
| `ZOTERO_LIBRARY_NOTE` | `Library` | title of the note the mirror creates when it may create one. |
| `ZOTERO_MAY_CREATE_ROOT` | `1` | set `0` on a shared vault, where apply must refuse rather than create a library. |
| `ZOTERO_SYNC_INTERVAL_S` | `1200` | the floor under the stream. |
| `ZOTERO_SYNC_ENABLED` | `1` | set `0` to stop the floor. |
| `ZOTERO_BUSY_RETRY_S` | `5` | wait this long before retrying a push that found the lock held. |
| `ZOTERO_RECONCILE_S` | `86400` | read a library whole when it has not been read whole for this long. This is the bound on how long a trashed paper can sit in the vault. |

## Verify

```
awm services list | grep zotero
awm zotero status
awm zotero apply --dry-run
```

`status` answers four separate questions: what the bundle holds, whether the
library is reachable, whether the stream is subscribed and how long it has been
quiet, and whether the bundle and the service disagree. An unreachable library
is reported, not raised — a network drops, and that is an answer rather than a
fault.

A dry run reports what it would create, update, replace and leave alone. On a
vault that is up to date it should say it would leave every paper alone. If it
says it would update all of them, `RENDER` moved or a fingerprint is wrong.

`awm zotero sync` logs where the pass spent its seconds, largest first. That is
how to tell a slow Zotero from a slow vault without guessing, and guessing got it
wrong once: a request was priced at 1.5 seconds across the board, when a page of
a hundred items costs about 1.6 and a window read costs about 0.3.

## Removing the mirror

Delete the shelf notes under the labelled root. Every mirror note's only branch
is a shelf, or a collection under one, so deleting the shelves removes the whole
mirror. The labelled note and its own children stay.

**CAUTION** There is one shelf per library that contributes items, not one per
library the account has. On this account that is two, not three. Deleting one
shelf leaves half the mirror behind.
