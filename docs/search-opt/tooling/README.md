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

## LanceDB benchmark

Run on 2026-11-05 against 10x journals: 21,690 entries in table `entries`, 200,890 chunk vectors in table `chunks_rq`. Scripts and raw results are at `altair:~/so_x10/lance/` and `mizar:~/.claude/jobs/5800850b/tmp/lancebench/`. Caches were capped at 64 MB index + 32 MB metadata. Cold means every dataset file was evicted with `posix_fadvise(DONTNEED)` before each query.

- **Layout.** `entries` has a full-text index with positions on title and body, BTREE on ts, LABEL_LIST on tags, BITMAP on project and scope. `chunks_rq` holds one row per chunk with IVF_RQ, 448 partitions. Build takes about 37 s on altair. The tables take 648 MB on disk.
- **Cold, altair** (15.6 MB/s sequential, slow random reads): phrase p50 1.2 s, p95 3.1 s. BM25 p50 1.2 s, p95 3.0 s. Vector p50 1.0 s, p95 1.8 s. Filter only p50 0.3 s, p95 1.4 s. Hybrid p50 1.5-2.2 s, p95 2.9-3.3 s. Each query reads 0.3-8 MB.
- **Cold, mizar** (SATA SSD): every category has p95 0.11 s or less. Hybrid p50 0.09 s.
- **First query after start** takes about 17 s on altair while the native library pages in.
- **RAM.** The query process peaks at 300-390 MB, Python and Arrow included.
- **Scale.** Cold hybrid p50 on altair is 0.47 s at 1x and 1.5 s at 10x.
- **Correctness.** Phrase hits equal a regex ground truth with 0 mismatches. Filters equal a numpy ground truth.
- **Vector recall@10** against brute force: IVF_RQ with nprobes 20 is 0.75 without refine and 0.96 with refine 5. Refine 5 raises cold hybrid on altair to p50 4.0 s and p95 6.2 s. IVF_PQ is worse at every setting.
- **Incremental writes.** Append, `merge_insert` update and delete each take under 0.1 s and read at most 6 MB. Searches see new, edited and deleted rows at once, before any `optimize`. Updated rows do not duplicate.
- **Optimize.** `optimize_indices(num_indices_to_merge=0)`, which adds a delta index, plus `compact_files` read 0.7-2.9 MB in about 0.1 s. Merging indices or a full `optimize()` reads the whole vector column, about 330 MB. Version cleanup is cheap.
- **Verdict.** LanceDB meets the spec with no major issue. Use delta index updates in normal operation and merge deltas rarely.
