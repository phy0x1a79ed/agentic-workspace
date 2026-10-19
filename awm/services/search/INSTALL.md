# Installing the `search` service

## Purpose & Contents

This file covers what the `search` service is, how to install it, and the contract a service meets to plug into its archive. It does not list verbs or arguments. Run `awm describe` with `tool=search` for those.

## What it is

`search` owns the `search` tool: one ranked search over everything the swarm knows, on this node and on its peers. It holds one hybrid index (`awm.persistence.embeddings`, unchanged) in `AWM_DIR/services/search/search.db`. Every document sits in one corpus under a `source:id` key, because the engine normalises scores per call.

## Install

    bash install.sh

The script installs the component libraries and this service into the `awm` env (override with `AWM_ENV=<name>`). It reuses the MiniLM model that `scopes` and `journal` already downloaded. It downloads nothing.

## Joining the archive

A service joins with one `[[archive]]` table per source in its `service.toml`. The gateway lists the tables under `archive` in each `awm registry_list` entry. `search` reads them there, so a new table appears in `search sources` with no change to this service.

- `name`: the source name. It is also the federation grant. Use lowercase letters, digits, `_` and `-`.
- `mode = "index"` with `export = "<tool>_export"`. The hidden export verb pages documents. Optional `topic` names an event that triggers a pass.
- `mode = "live"` with `query = "<tool>_<verb>"`. The verb takes `query`, `limit` and the common filters. It returns `{hits: [{id, title, snippet, ts?, project?, scope?, kind?, url?}]}`. `search` calls it at query time. Optional `optin = true` runs the source only when a caller names it in `sources`.

The export verb takes `cursor` and `limit` (default 200, clamped to 1-1000). It returns `{documents: [{id, title, text, ts, project?, scope?, kind?, url?, tags?}], next_cursor, done, ids}`. `tags` is a list of strings.

- Keys ascend. The cursor is the last key sent.
- `ids` appears only on the final page, with every key. `search` drops stored documents missing from it.
- `ts` is ISO 8601 UTC.
- **CAUTION** An export that cannot read its store must raise. An empty `ids` from a broken store deletes the source's documents.

## Behaviour to know

- A pushed event schedules a full export pass a few seconds later. An hourly pass runs regardless.
- A source whose service stops is frozen. Its documents stay searchable. A sweep never reads an absent service as deletion.
- A foreign peer reaches the read verbs only with at least one grant. The gateway passes the grants as `args["_grants"]`, and `search` answers only those sources. A call that arrives from a peer searches this node only.
- A peer never sees a document under a private mark (`awm.config.privacy`). `search` matches a document by its `project` and `scope`, in every journal spelling `privacy.canonical` reads, and by a tag equal to a marked project's name, ignoring case. A live hit also matches by a transcript slug in `project`. The filter runs in the index query, so a page stays full. The `sources` and `status` counts leave the hidden documents out. `get` answers `no document` for one.
- **CAUTION** A document with no `project`, `scope` or marked tag stays visible to peers whatever its text says, such as an untagged precedence decision that mentions a private project. An unreadable marks file hides from peers every document that names a project, a scope or a tag.
- A peer on a release without `search` reports `unsupported` and never hides local hits.
