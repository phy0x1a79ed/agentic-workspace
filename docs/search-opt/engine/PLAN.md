# build: plan for LanceDB as the search engine

## Context

The interview in `docs/search-opt/` settled the spec, chose LanceDB and passed its cold benchmark on journals. This plan builds it into the `search` service on `feat/redux-search-opt`. The outcome is a search service that keeps each indexed source in its own LanceDB store, follows each source through a change feed, serves the signature with its settled changes, and hot-swaps onto the fleet through redux-build/dev.

## What you said

> If no major issues, compact and implement lance db as engine for the search service.

> Just using lance db plus its incremental updates is fine for now.

> I accept your feed source suggestions

> Adding sources needs to be generic since columns can change.

> For search, possible to just hotswap it in once new version done right? If so, lets do that

> please dont start capella and dont modify mira

## Issues

- I1. Search keeps journals in one SQLite table and reads every stored vector into memory on each `$about` query, so query cost and peak memory grow with the corpus.
- I2. Search learns about journal changes only by paging the whole export every hour and on each push.
- I3. The fields each source accepts are hard-coded in `query.FIELDS` and `compile.py`, so a new source or a new field needs code changes in search.
- I4. A hit is fixed to `{id, title, ts}`, sections list newest first, and no caller can sort.
- I5. `$contains` uses a trigram substring index that the spec replaced with a phrase match on words.
- I6. The journal ranking leans on a second engine, kb's entry index, where the spec chose one engine.

## High-level goals

- G1. Journals search from disk with little steady memory and a latency that holds as the corpus grows.
- G2. A source declares its own fields, and search builds its store and its filters from that declaration.
- G3. Search follows each indexed source incrementally and never rescans it in normal operation.
- G4. The query surface carries the settled signature changes.
- G5. The new search swaps in with an ordinary deploy and rebuilds its own store on start.

## Acceptance criteria

- Every `[[service.archive]]` table declares `fields`, each field a class of `content`, `categorical`, `numeric` or `datetime`, and `hit`, the fields a hit shows. `search sources` lists them. `query.FIELDS` is gone.
- An index source keeps a LanceDB table of entries and a table of chunk vectors under `AWM_DIR/services/search/lance/`. The SQLite `documents`, `documents_exact`, `passages`, `passages_words`, `doc_values` and `embeddings` tables are gone from `search.db`.
- `journal_export` takes `since` and returns the changes after it, deletions included, with an `epoch`. Search stores the last number per source and asks only for what follows. An epoch change, a field change or an engine-version change rebuilds that source from 0 without a manual step.
- An edit, retag or delete in the journal reaches search within one settle interval of its push, and a query sees it before any index optimize.
- `$contains` is a case-insensitive phrase match on words over all content fields, with punctuation as a word boundary: `p95-latency` finds `p95 latency`.
- `$sort` in a source filter takes one field with `+` or `-`. With no `$about` it selects and orders. On a content field it orders by match score. With no `$sort` and no `$about`, a section returns its first `max` matches unsorted.
- A hit is the id plus the source's `hit` fields.
- The query description carries `$sort in F: one field, +/-. On content, by match score.` and the new hit line, and stays in its style.
- `scripts/replay.py` over the stored judgement sets shows no recall loss against the current engine.
- The search tests pass under `scripts/run-tests.sh`. The journal tests that touch the export pass.
- Cold queries on mizar's journals stay within the benchmark's mizar figures. The search process stays under 1 GB RSS, model included.
- The restart set and the rebuild time on mizar and altair reach redux-build/dev and Swarm triage before the merge.

## Tasks

- T1. Write this plan (G1-G5).
- T2. Add the journal change feed (G3).
- T3. Declare fields in each archive table and derive the query schema from them (G2, G4).
- T4. Build the LanceDB store layer (G1, G2).
- T5. Build the query layer on the store (G1, G4).
- T6. Compact.
- T7. Wire the sync, the maintenance and the start-up rebuild, and remove the old index code (G3, G5).
- T8. Rewrite the tests and run the replay gate (G1-G4).
- T9. Commit a checkpoint and push the branch.
- T10. Compact.
- T11. Measure the rebuild on mizar and altair, report the restart set, and hand the branch to redux-build/dev (G5).
- T12. Draft the doc updates (G2, G5).
- T13. Debrief.

## Approach by task

### T1. Write this plan

This file. It rests on reading `archive.py`, `query.py`, `compile.py`, `index.py`, `store.py`, `registry.py`, `hub_adapter.py`, the journal's `export.py` and `search_index.py`, and on a probe of LanceDB 0.39. The probe showed nested `BooleanQuery` over `PhraseQuery`, a phrase across hyphenated words, `array_has` and `regexp_like` in a prefilter, and `id IN (...)` all work. A `BooleanQuery` with only `MUST_NOT` is refused.

Gotchas: none left for execution.

### T2. Add the journal change feed

The journal service gains a `search_feed` table: one row per thread root with the sequence number of its last change, a counter, and an epoch id made when the table is made. Every place that already calls `search_index.index_post` also bumps the root. Tag rename, retag, fold and delete bump each root they touch. The first start seeds every indexed root. `journal_export` takes `since` and `limit`. It answers `{changes: [{seq, id, entry?}], next, done, epoch}`. A root that no longer qualifies, or that the private exclusion hides, goes out with no `entry`, which means deleted. The epoch folds in a hash of the private exclusion, so a change to the private marks rebuilds the source once. The entry uses the declared field names: `title`, `text`, `ts`, `project`, `scope`, `tag`, `replies_to`. A project channel goes out as its project with no scope, which retires the `project:<p>` scope quirk.

Gotchas: a bump must land in the same transaction as the write it reports, or a crash leaves a change the feed never shows. A tag rename touches many roots, so bump them in one statement. The journal's own search index stays as it is.

### T3. Declare fields in each archive table and derive the query schema from them

Each `[[service.archive]]` table gains a `fields` table, name to class, and a `hit` list. A categorical field may set `nested = "/"`, so a value covers the values below it, which keeps "a scope covers its nested scopes". Every categorical holds one or more values. `registry.py` reads and checks the declarations. The query parser takes its field schema from the declarations of every manifest on disk, so `kb@altair` validates on a node that has no kb. `$sort` becomes a reserved key in a source filter, checked against the source's fields. `hits.clean` keeps the id plus the kind's `hit` fields. kb and transcripts declare the fields their live verbs read today. The description gains the `$sort` line, the new hit line, and `$contains` as a phrase match.

Gotchas: a remote kind that no local manifest declares cannot be validated here, so forward it and let the peer refuse. A field named `id` or starting with `$` must be refused at registry read.

### T4. Build the LanceDB store layer

A new `lance_store.py` owns one LanceDB connection under `AWM_DIR/services/search/lance/`, with the 64 MB index and 32 MB metadata caps and a zero read-consistency interval, so a read sees the latest write. Each index source gets two tables. `<source>` holds one row per entry: `id`, each content field as a string with a positional full-text index, each categorical as a list of strings with a label-list index, each numeric as a float, each datetime as a UTC timestamp with a B-tree. `<source>__chunks` holds one row per chunk: `id`, `chunk`, a hash of its text, the 384-dimension vector, and a copy of every non-content field so a vector search can prefilter. Chunks come from the existing MiniLM chunker in `awm.persistence.embeddings`. An upsert replaces an entry with `merge_insert` and its chunks with a delete and an add. It reuses a stored vector whose chunk hash is unchanged, so a retag embeds nothing. The IVF_RQ index appears once a chunk table passes 5,000 rows, with the square root of the row count as partitions. Below that a flat scan is exact and small. `search.db` keeps `sources`, `judgements`, `usage` and `usage_score`. A schema migration drops the old tables. Each source row records its sequence number, epoch, field hash and engine version.

Gotchas: `lancedb` is not in the `awm` env. Pin `lancedb==0.39.0`, the benchmarked version, in `pyproject.toml` so `awm deploy` installs it. The first query after start pages in the native library for about 17 s on altair, so run one tiny query at start. Writes stay on the one index worker thread.

### T5. Build the query layer on the store

A new `lance_query.py` compiles a validated filter into a Lance SQL prefilter. A categorical equality is `array_has`. A glob or a nested value is `regexp_like` over the joined list. A numeric or datetime is a comparison. `$ne` and `$nin` match an entry without the field. The `$contains` tree compiles to a `BooleanQuery` of `PhraseQuery` legs, one leg per content field per literal. A tree with no positive leg scans the filter's matches and drops those the tree's negation finds. The private-mark veil compiles to the same SQL over `project`, `scope` and `tag`. Each section is built this way:

1. No text: the filter's matches, sorted by `$sort` when given, else the first `max`.
2. `$contains` only: the phrase query under the filter. A content `$sort` orders by its BM25 score.
3. `$about` alone or under `$and`: the matches ranked by reciprocal rank fusion of a vector search on chunks and any-word BM25 on entries, both under the same prefilter. With `$contains`, both legs are limited to the phrase matches, up to 1,000.
4. `$about` under `$or`: `exact` as in 3, and `related` as the fused ranking under the filter alone, less every hit that the phrase query matches.

With `$about`, relevance selects the top `max`, and a `$sort` on a non-content field reorders them. The kb entry index leg and `meaning.py` go. The probe count and refine factor are constants. T8 sets them to the cheapest pair that loses no recall in the replay.

Gotchas: a vector search over chunks returns several chunks per entry, so ask for three times `max` and collapse to entries. A phrase with no word characters matches nothing, so refuse it as an empty `$contains`. A lone `MUST_NOT` must never reach Lance.

### T6. Compact

Handoff: the plan at `docs/search-opt/build/PLAN.md`, T2-T5 committed on `feat/redux-search-opt`, the todo list at T7. Carry the probe facts in T1 and anything T2-T5 found that the code does not show.

### T7. Wire the sync, the maintenance and the start-up rebuild, and remove the old index code

`Archive._pass` becomes a feed read: ask the export for changes after the stored number, upsert or delete each, store the new number per page. A push still schedules a read after the settle delay. The hourly timer becomes an hourly feed read, not a rescan. On start, and on each registry refresh, a source whose engine version, field hash or epoch differs drops its tables and reads from 0. After each batch of writes, maintenance runs `optimize_indices(num_indices_to_merge=0)` and `compact_files` on both tables. It merges the delta indexes once more than eight stack up, and cleans versions older than an hour. `index.py`, `compile.py`, `passages.py`, `meaning.py` and the SQLite document code in `store.py` go. `reindex` rebuilds a source from 0.

Gotchas: a merge or full optimize reads the whole vector column, about 330 MB at 10x journals, so it must run on the worker and never block a query. A source whose service is down stays frozen and keeps serving. A failed page must not advance the stored number.

### T8. Rewrite the tests and run the replay gate

Replace the tests that drive the SQLite index with tests over the Lance store and query layer: the filter compiler against every operator, glob and nesting, the phrase tree, each section rule, `$sort`, the veil, the feed sync with edits and deletes, the rebuild on a field or epoch change, and hit shapes from live sources and peers. Extend the journal export tests to the feed. Run `scripts/replay.py` against the judgement sets on mizar's journals, before and after, and pick the probe count and refine factor.

Gotchas: tests need a small embedder stub, or the model load dominates the run. The replay must use the same journal snapshot for both engines.

### T9. Commit a checkpoint and push the branch

Commit the code with its tests. Push `feat/redux-search-opt` to `github`.

Gotchas: never commit in the workspace root checkout.

### T10. Compact

Handoff: the branch pushed, the replay result and the chosen probe settings, the todo list at T11.

### T11. Measure the rebuild on mizar and altair, report the restart set, and hand the branch to redux-build/dev

On mizar and on altair, build the store from a copy of the node's journal database with the new code outside the running service, and time it. Report the restart set (search, journal, and a gateway refresh for the changed manifests) and both rebuild times to redux-build/dev and to Swarm triage (8162a704). redux-build/dev merges the branch into dev, and the routine deploy swaps it in.

Gotchas: do not start capella or touch mira. Do not restart the live search on any node for the measurement.

### T12. Draft the doc updates

Rewrite `awm/services/search/INSTALL.md` for the field declarations, the change-feed export contract and the Lance store. Record the build decisions in `docs/search-opt/build/README.md`. Delete what the change made untrue.

Gotchas: describe the code as it stands, not the session.

### T13. Debrief

Run the `retire` skill: debrief, report to Swarm triage, stop the session.

## Callouts

- kb and transcripts stay live sources in this build. They gain field declarations, so each moves into a Lance store later by adding a change feed, with no change to the mechanism. Tony said papers are along for the ride.
- With `$about`, a `$sort` on a non-content field reorders the top `max` by relevance rather than selecting by that field. This keeps `$about` meaningful under a sort.
- A change to the private marks rebuilds the journals store once, through the epoch. It reuses no vectors across a rebuild, so it costs one full embed.
