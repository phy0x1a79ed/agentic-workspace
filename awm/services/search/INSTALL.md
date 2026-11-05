# Installing the `search` service

## Purpose & Contents

This file covers what the `search` service is, how to install it, and the contract a service meets to plug into its archive. It does not list verbs or arguments. Run `awm describe` with `tool=search` for those.

## What it is

`search` owns the `search` tool: one search over the sources a caller names, each as `kind` on this node or `kind@node` on another. Each source answers apart. The `query` verb's description states the call.

An index source lives in LanceDB under `AWM_DIR/services/search/lance/`, as two tables: `<source>` holds one row per entry, and `<source>__chunks` holds the entry's text chunks with their MiniLM vectors. `AWM_DIR/services/search/search.db` holds each source's feed cursor, the judgement set and the usage log. `docs/search-opt/engine/README.md` records why the engine has this shape.

## Install

    bash install.sh

The script installs the component libraries and this service into the `awm` env (override with `AWM_ENV=<name>`). `pyproject.toml` pins `lancedb` and `pylance`. `awm deploy` runs this script through `awm/install.sh`, so a deploy brings each node's env to the pins. The script reuses the MiniLM model that `scopes` and `journal` already downloaded.

## Joining the archive

A service joins with one `[[service.archive]]` table per source in its `awm-plugin.toml`. The gateway lists the tables under `archive` in each row of `awm services`. `search` reads them there, so a new table appears in `search sources` with no change to this service.

- `name`: the source name. It is also the federation grant. Use lowercase letters, digits, `_` and `-`.
- `[service.archive.fields]`: each field the source serves, as `name = "<class>"`. The classes are `content`, `categorical`, `numeric` and `datetime`. A categorical field that nests, such as a scope, is `{ class = "categorical", nested = "/" }`. The declaration is the query schema: a caller filters on these fields and no other.
- `hit`: the declared fields a hit shows beside its id.
- `mode = "index"` with `export = "<tool>_export"`. The hidden export verb is the source's change feed. Optional `topic` names an event that brings the next read forward.
- `mode = "live"` with `query = "<tool>_<verb>"`. The verb takes `query` (one string), `limit` and the filter fields that `archive.live_args` translates. It returns `{exact: [hit], related: [hit]}`. A bare `{hits: [hit]}` counts as `exact`. `search` calls it at query time.
- **CAUTION** A live source serves only the fields `live_args` translates. Every other declared field returns that source's `error`, "not yet served", until the source takes the filter itself.
- `get = "<tool>_<verb>"` on a live table. The verb takes `id` and returns the whole document. Without it, `search get` cannot read that source's hits.

The export verb takes `since` and `limit`. It returns `{changes: [{id, entry?}], next, done, epoch, reset?}`.

- `entry` holds the declared fields. A change without `entry` deletes the id. An entry whose content fields are all empty also deletes it.
- `next` is the cursor of the last change sent. `search` stores it only after it writes the page, so a read that dies resumes at the last page it wrote.
- `epoch` names the feed's history. A new epoch makes `search` drop the source and read it from the start. `reset: true` does the same, for a cursor past the feed's head.
- `ts` is ISO 8601 UTC.
- **CAUTION** The feed must show every write that changes an entry, a tag rename included. A missed change stays stale in the index until the next rebuild. The journal bumps its feed with triggers in the same transaction as the write.
- **CAUTION** An export that cannot read its store must raise. A failed read changes nothing and retries a minute later.

## The awm source

`search` declares one source of its own, `awm`: one entry per tool and verb that this node's gateway describes, with the verb's description and params as text. Its feed is the hidden `search_api_export` verb, which reads the whole table through `describe` on each read. The feed's epoch is a digest of the table, so a changed table rebuilds the source.

- A change in the `awm services` rows, such as a service that restarts or registers, brings the next read forward.
- `get` on an `awm` hit calls `<tool> describe` with that one verb and returns its answer. The index holds only what a query ranks.
- **CAUTION** A query or `get` that a peer relays leaves out each singleton tool whose callers in `edges.yml` leave that peer out (`fleet.md` R98 item 3). A count in `search sources` still includes those tools.

## Rebuilds

`search` rebuilds a source from the start of its feed when the source's fields, its hit, the embedding model or `lance_store.ENGINE_VERSION` change, when the epoch changes, and on `search reindex`.

- **CAUTION** While a source rebuilds, `query` answers it with the error "not built yet" until the first page lands. Then it answers from what is written so far. A rebuild embeds every entry. Measured times are in `docs/search-opt/engine/README.md`.
- Bump `ENGINE_VERSION` for any change to the table layout or to how rows are written. Without the bump, nodes keep tables in the old layout.

## Behaviour to know

- A pushed event brings the next read forward to a few seconds later. An hourly read runs regardless. A read pages from the stored cursor, so it costs only the changes since the last one.
- One worker thread writes the store. After a read that wrote, it merges small fragments and builds the vector index once a source passes 5000 chunks.
- A source whose service stops is frozen. Its entries stay searchable. A read never takes an absent service as deletion. A source that turns live loses its tables.
- A foreign peer reaches the read verbs only with at least one grant. The gateway passes the grants as `args["_grants"]`, and `search` answers only those sources. A call that arrives from a peer searches this node only.
- A peer never sees an entry under a private mark (`awm.config.privacy`). `search` matches an entry by its `project` and `scope`, in every journal spelling `privacy.canonical` reads, and by a tag equal to a marked project's name, ignoring case. A live hit also matches by a transcript slug in `project`. The filter runs inside the store query, so a page stays full. The `sources` counts leave the hidden entries out. `get` answers `no document` for one.
- **CAUTION** An entry with no `project`, `scope` or marked tag stays visible to peers whatever its text says, such as an untagged journal post that mentions a private project. An unreadable marks file hides from peers every entry that names a project, a scope or a tag.
- An open tag clash between this node and another, as the journal's `tag_list` records it, refuses every search and `get` between the two. A tag filter means one thing only while both nodes agree on its names.
- A hit names its entry and quotes none of it. `search` drops every key outside the id and the source's `hit` fields, so neither a source nor a peer can add text to a hit.
- Each `search get` writes one row to the usage log in `search.db`: the time, the caller's project and scope from the gateway's `_caller_scope`, the query, the source, the document id and the node. The same commit folds the get into the item's `usage_score.s` (`fleet.md` R136 item 6). No rank reads either yet. Turn a usage rank on only after a replay of the log shows it beats the plain order.
- `scripts/replay.py` replays the judgement set through two search services and checks phrase recall on the stored text. Run it before and after any change to ranking.
