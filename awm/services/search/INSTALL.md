# Installing the `search` service

## Purpose & Contents

This file covers what the `search` service is, how to install it, and the contract a service meets to plug into its archive. It does not list verbs or arguments. Run `awm describe` with `tool=search` for those.

## What it is

`search` owns the `search` tool: one search over the sources a caller names, each as `name` on this node or `name@node` on another. Each source answers apart. It holds one hybrid index (`awm.persistence.embeddings`, unchanged) in `AWM_DIR/services/search/search.db`. Every document sits in one corpus under a `source:id` key, because the engine normalises scores per call.

## Install

    bash install.sh

The script installs the component libraries and this service into the `awm` env (override with `AWM_ENV=<name>`). It reuses the MiniLM model that `scopes` and `journal` already downloaded. It downloads nothing.

## Joining the archive

A service joins with one `[[archive]]` table per source in its `service.toml`. The gateway lists the tables under `archive` in each `awm registry_list` entry. `search` reads them there, so a new table appears in `search sources` with no change to this service.

- `name`: the source name. It is also the federation grant. Use lowercase letters, digits, `_` and `-`.
- `mode = "index"` with `export = "<tool>_export"`. The hidden export verb pages documents. Optional `topic` names an event that triggers a pass.
- `mode = "live"` with `query = "<tool>_<verb>"`. The verb takes `query`, `limit` and the common filters. It returns `{exact: [hit], related: [hit]}`, each hit `{id, title, ts?, project?, scope?, kind?, url?, match?}`. A bare `{hits: [hit]}` counts as `related`. `search` calls it at query time. Optional `optin = true` runs the source only when a caller names it in `sources`.
- `get = "<tool>_<verb>"` on a live table. The verb takes `id` and returns the whole document. Without it, `search get` cannot read that source's hits.
- `hop = "<tool>_<verb>"` and `links = [...]` on a live table. The verb takes `ids`, `along` (`cites`, `cited_by` or `authors`), `limit`, `after` and `before`, and returns `{hits: [hit]}` in its walk's order. `links` names the `hop` link types the source answers: `citations`, `cited_by` or `authors`. An index source answers `reply_to` from its export's `reply_to`.

The export verb takes `cursor` and `limit` (default 200, clamped to 1-1000). It returns `{documents: [{id, title, text, ts, project?, scope?, kind?, url?, tags?, reply_to?}], next_cursor, done, ids}`. `tags` is a list of strings. `reply_to` lists the ids of the documents it replies to or cites.

- Keys ascend. The cursor is the last key sent.
- `ids` appears only on the final page, with every key. `search` drops stored documents missing from it.
- `ts` is ISO 8601 UTC.
- **CAUTION** An export that cannot read its store must raise. An empty `ids` from a broken store deletes the source's documents.

## Behaviour to know

- A pushed event schedules a full export pass a few seconds later. An hourly pass runs regardless.
- A source whose service stops is frozen. Its documents stay searchable. A sweep never reads an absent service as deletion.
- A foreign peer reaches the read verbs only with at least one grant. The gateway passes the grants as `args["_grants"]`, and `search` answers only those sources. A call that arrives from a peer searches this node only.
- A peer never sees a document under a private mark (`awm.config.privacy`). `search` matches a document by its `project` and `scope`, in every journal spelling `privacy.canonical` reads, and by a tag equal to a marked project's name, ignoring case. A live hit also matches by a transcript slug in `project`. The filter runs in the index query, so a page stays full. The `sources` and `status` counts leave the hidden documents out. `get` answers `no document` for one.
- **CAUTION** A document with no `project`, `scope` or marked tag stays visible to peers whatever its text says, such as an untagged journal post that mentions a private project. An unreadable marks file hides from peers every document that names a project, a scope or a tag.
- A node on a release without `search` reports `no-search` and never hides local hits.
- An open tag clash between this node and another, as the journal's `tag_list` records it, refuses every search and `get` between the two. A tag filter means one thing only while both nodes agree on its names.
- A hit names its document and quotes none of it. `search` drops every key outside the hit shape above, so neither a source nor a peer can add text to a hit.
- Each `search get` writes one row to the usage log in `search.db`: the time, the caller's project and scope, the query, the source, the document id and the node. No rank reads the log yet. Turn a usage rank on only after a replay of the log shows it beats the plain order.
