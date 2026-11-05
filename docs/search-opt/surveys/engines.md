# engines: survey of integrated search engines against the spec

## Purpose & Contents

This file holds the read-only survey, run on 2026-11-05, of integrated engines scored against `spec/README.md`. It feeds the **tooling** topic. It is not a decision.

## Scores

Criteria: (1) on disk with small steady RAM, (2) stripped substring, (3) vectors on disk, (4) typed filters, (5) schema changes and updates, (6) embeddable, (7) cold-cache evidence. A `*` marks a claim from memory.

| Tool | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| LanceDB | Yes. Its cache defaults to 6 GB index + 1 GB metadata and must be capped | NGRAM index, FM-index, ngram tokenizer | IVF-RQ/PQ, a few large partition reads | SQL, LABEL_LIST for list fields, LIKE | add/alter/drop columns, merge_insert. Needs `optimize()` and version cleanup | Yes | None. Vendor full-text p95 about 3 s |
| SQLite FTS5 + sqlite-vec / vec1 | Yes | FTS5 trigram, with a budget on common trigrams | sqlite-vec scans. vec1 IVF is unreleased | JSON functions or side tables | In place | Yes | Measured internally |
| Postgres + pg_trgm + VectorChord | Small shared buffers possible | pg_trgm on a stripped expression | IVF + RaBitQ | Richest | MVCC, jsonb | No, a server | None |
| DuckDB fts + vss | No: HNSW loads into RAM | None | Experimental | Good | fts never updates | Yes | None |
| Tantivy | Yes, mmap | Ngram tokenizer | None | JSON and fast fields | Immutable segments | Yes | Fair |
| Qdrant | Tiers memory | No ngram | HNSW, slow cold tails per its docs | Rich | Upsert | Server, Edge is early | Slow cold |
| Meilisearch | Leans on page cache | Experimental CONTAINS | Graph | Good | Upsert | Server | None |
| Typesense | No: index in RAM | — | — | — | — | Server | — |
| Manticore | HNSW in RAM | infix* | In RAM | SQL | Real-time | Server | None |
| Infinity | Unclear* | ngram analyzer | HNSW/IVF | SQL-ish | Yes* | Embedded SDK, young | None |
| Vespa | No: attributes in RAM, JVM | Gram matching | HNSW in RAM | Rich | Partial | Heavy server | — |
| Elasticsearch / OpenSearch | No: JVM heap | wildcard field, ngram | HNSW, OpenSearch disk mode | Rich | Yes | Server | — |
| Milvus | Heavy | NGRAM, case-sensitive | DiskANN | Good | Upsert | Server | — |
| Weaviate* | HNSW in RAM | Wildcard scan | flat + BQ | Good | Yes | Server | — |
| Chroma* | No: HNSW in RAM | SQLite trigram | In RAM | Mongo-like | Yes | Yes | — |
| zvec | Yes | N-gram tokenizer (v0.7.0) | DiskANN, IVF-RaBitQ with pread | Undocumented | Unclear | Yes | None |

## Conclusions

- No engine meets the spec whole. Every engine lacks cold-cache evidence on a disk like altair's, the Mongo filter syntax, and the `exact`/`related` sections with BM25 blending.
- Strict stripped-substring needs no engine support. Store a shadow field per content field, lowercased with non-alphanumerics removed. Normalize the query the same way. Run a substring or trigram match on the shadow field.
- LanceDB is the best partial fit. One embedded library covers every criterion. Its risks are cache defaults, regular `optimize()` and version cleanup, and no cold measurement. An FM-index query makes about 40 dependent reads, about 2 s cold on altair, by estimate.
- SQLite is the measured fallback: FTS5 with 64 KB pages, trigram on the shadow field, and an own IVF on exact pread (195 ms p50 cold in 7bf7afdc).
- The gap on top of LanceDB or SQLite: map field classes to columns, compile Mongo filters to SQL, write the query layer (exact and related legs, fusion, `$sort`, `max`, hit fields), and run maintenance jobs.
- A cold benchmark on altair decides between candidates: journals at 10x and papers at 5M chunks, page cache dropped, for NGRAM, FM, BM25 and IVF-RQ.
- Verdict: gap-fill around an existing engine. Build ground up only if LanceDB and SQLite both fail cold.

## Sources

- https://docs.lancedb.com/indexing/scalar-index
- https://lancedb.github.io/lance/format/table/index/scalar/ngram
- https://docs.rs/lance-index/10.0.0/src/lance_index/scalar/fmindex.rs.html
- https://lancedb.com/docs/search/full-text-search
- https://sqlite.org/vec1/doc/trunk/doc/vec1.md
- https://docs.vectorchord.ai/vectorchord/getting-started/overview.html
- https://qdrant.tech/documentation/production-operations/memory-tiers/
- https://zvec.org/en/blog/2026-08-25-zvec-release/
