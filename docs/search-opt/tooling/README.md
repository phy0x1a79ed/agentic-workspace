# tooling

## Purpose & Contents

This file holds whether existing tools meet the spec, in whole or in part, and the shape of the gap left to design: gap-fill around a tool or a ground-up build.

## Survey

Three read-only surveys ran on 2026-11-05. Each file holds its table and sources.

- `../surveys/engines.md`: integrated engines. No engine meets the spec whole. LanceDB is the best partial fit: embedded, phrase and BM25 search, IVF-RQ vectors, typed and list columns, column changes and merge updates. Its caches must be capped. It needs regular `optimize()`. It is unmeasured cold. SQLite FTS5 plus an own IVF on exact pread is the measured fallback.
- `../surveys/substring.md`: exact matching. The relaxed form is native in FTS5, Tantivy, Lucene, Postgres and LanceDB. The strict form needs a second index on a stripped shadow column, such as FTS5 trigram queried by its rarest trigrams.
- `../surveys/bio.md`: BLAST, DIAMOND and kin. None fits as an engine: they assume batches, RAM or static data. Their seed-index ideas (rarest q-grams, minimizers, volumes with tombstones) would serve a strict-form build.

The gap on top of LanceDB or SQLite is glue: map field classes to columns, compile Mongo filters to SQL, write the query layer (exact and related legs, fusion, `$sort`, `max`, hit fields), and run maintenance jobs. No survey found cold-cache evidence on a disk like altair's. A cold benchmark decides between the candidates.

## Decisions

- 2026-11-05: Exact matching may bend to keep spaces when that lets an existing method serve it. The relaxed form is a case-insensitive phrase match on words, with punctuation as a word boundary: `p95-latency` matches `p95 latency`, but `p95latency` does not. Tony: "We can bend exact matching if existing methods keep spaces".
- 2026-11-05: Take the relaxed exact match and gap-fill around a prebuilt engine. Search translates queries to the engine and results back from it. Choose one engine. Do not keep a fallback. Tony: "Accept, but i want to choose rather than have fallbacks." and "Gap filling makes sense, i guess we translate to and from a prebuilt engine dependency."
- 2026-11-05: The engine is LanceDB. A test against journals, real or mock, checks it. Papers are optional extra data. Tony: "Try lancedb. Just run tests against journals (or mock journals). Dont really care about papers. Can test papers for extra data".
