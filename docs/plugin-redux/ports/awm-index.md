# Port plan: awm-index

The search engine each source links, built from search's `lance_store.py` and `lance_query.py`. journal and guide use it, and kb or transcripts can adopt it later.
- **Tables:** per source, an entries table and a chunks table with 384-dimension vectors keyed by `sha1("1\0"+text)`. The positional FTS, label-list, BTREE and BITMAP indexes and IVF_RQ at 5,000 chunks keep the redux layout (`docs/search-opt/engine/`).
- **Query layer:** the `query` grammar's per-source part (`$about`, `$contains`, filters compiled to DataFusion SQL), the exact and related sections, min-max fusion (alpha 0.4, depth 100), `max` and the hit fields. The caller passes the query vector, so a source loads the model only to embed its own writes. The caller also passes a veil predicate built from the marks.
- **Yearly sections:** each source keeps one table pair per calendar year of the entry's own date. The first write dated in a year with no section creates it. A query runs on every section, newest first, and fuses the candidates. Each section is normalised min-max first, because BM25 statistics differ between sections. An update or delete finds its row through the id's BTREE in each section.
- **Maintenance on write events.** No clock runs. After each committed write, the writer updates per-section counters:
  - 500 or more rows outside any index: a delta index plus `compact_files`, on the worker.
  - More than 8 delta indexes: merge them.
  - A section that has doubled since its vector index was trained: retrain.
  - Each compaction ends by removing the versions it superseded.
- **The freeze:** with no model, a write stores the entry and marks its chunks waiting. The model loading is the event that embeds them. No vector is ever written as NULL.

Tests: the redux's engine suite ported, a section boundary, a counter crossing each threshold, no maintenance with no writes, and the freeze.

The T6 spike built `lancedb` 0.41 (no default features) as a static musl binary and checked it against a copy of capella's search store with its IVF_RQ index. It matched the Python's answers: the FTS top 10 with equal scores, the label-list ids, and the vector top 30 in the same order. The vector `_distance` is half the Python's, because Rust reports `1 - cos` and pylance 14.0.2 through lancedb 0.39 twice that. Min-max fusion cancels the scale. The build needs `protoc` and the well-known protos in the build image. The spike binary is 238 MB stripped.
