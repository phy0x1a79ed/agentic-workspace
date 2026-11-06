# substring: survey of disk-based exact matching against the spec

## Purpose & Contents

This file holds the read-only survey, run on 2026-11-05, of tools for the spec's exact match in its strict form (stripped substring) and its relaxed form (phrase match on words). It feeds the **tooling** topic. It is not a decision.

## Size baseline

Estimated, not measured. 10x journals hold about 90 MB of raw text, about 75 MB stripped, about 25 MB with zstd.

- A scan of 75 MB takes 4.4 s at 17 MB/s and 0.19 s at 400 MB/s.
- A scan of 25 MB zstd takes about 1.5 s at 17 MB/s. It breaks past about 13x.
- A scan grows with the corpus, so it breaks the latency rule eventually. A rare string reads everything.

**CAUTION** SQLite, mmap'd Tantivy, Zoekt and livegrep fault pages in one at a time. At 17 reads/s, 18 trigram lookups plus 20 candidate checks take about 2.2 s. Parallel reads happen only when our own code issues them.

## Tools

| Tool | Strict | Relaxed | Cold query | Update | Size / RAM | Py/Rust |
|---|---|---|---|---|---|---|
| SQLite FTS5 trigram on a stripped column | `LIKE '%q%'` with recheck, or a custom stripping tokenizer | unicode61 + phrase MATCH, off the shelf | About 18 + N reads at 10x journals. Papers need a trigram budget | In place. Periodic `optimize` + VACUUM | detail=full about 3x text. Small RAM | Both |
| pg_trgm GIN | Expression index on stripped text | tsvector `<->` | Reads one block at a time* | GIN pending list | Server | Client |
| Zoekt | On pre-stripped text. Rarest trigram pair first | Regex | mmap faults one at a time | Shard rebuild | About 3x text | Go server |
| codesearch / Hound | Doc-level trigrams, then grep | Regex | Few lists, then candidates | Rewrite | About 20% of text | Go |
| Tantivy ngram | AND grams, verify from doc store | PhraseQuery, off the shelf | mmap faults one at a time | Immutable segments | Moderate | Rust |
| Elasticsearch wildcard | Trigrams + doc-value verify | Phrase | — | Segments | JVM heap, fails RAM | Server |
| ClickHouse ngrambf | Bloom per granule | Token index | Reads every bloom, grows linearly | Costly | Deprecated | chDB |
| livegrep | Suffix array | Regex | Best in RAM | Static | Fails RAM | C++ |
| FM-index / suffix array | Exact | — | 40-52 dependent reads, about 2.4-3 s | Static | 300-600 MB in RAM | Both, in RAM |
| ripgrep / memmem scan | On stripped text | Regex | Whole file | Append | Text size | Both |

A `*` marks a claim from memory. Also from memory: the FTS5 AND order, tantivy-py tokenizer support, sdsl build RAM, and the journal entry size.

## Conclusions

- Strict form: no tool fits off the shelf. A thin gap-fill on SQLite FTS5 trigram covers it.
  1. Store a stripped, case-folded copy of each entry, one row per entry, rowid in time order, detail=none.
  2. Hold each trigram's document frequency in memory. A 36-letter alphabet has at most 46,656 trigrams, under 1 MB.
  3. MATCH only the 2-4 rarest trigrams. Verify with `instr`. Stop at `max` verified hits.
  4. Prefetch candidate pages at QD32 from our own threads.
  5. Run `optimize` + VACUUM with 64 KB pages on a schedule.
- That costs about 4 + N reads at 10x journals, under a second. A query under 3 characters falls back to a scan.
- Relaxed form: FTS5 unicode61 phrase MATCH, Tantivy or Lucene PhraseQuery, and Postgres tsvector serve it off the shelf. It reads 2-3 word lists in place of about 18 trigram lists, with no verify step. It loses mid-word matches and the joined-or-split equivalence.

## Sources

- https://www.sqlite.org/fts5.html
- https://swtch.com/~rsc/regexp/regexp4.html
- https://gerrit.googlesource.com/zoekt/+show/a9b6bda45559b9c4ee6ae6bcb5945365fa8d0e56/doc/design.md
- https://blog.nelhage.com/2015/02/regular-expression-search-with-suffix-arrays/
- https://www.elastic.co/blog/find-strings-within-strings-faster-with-the-new-elasticsearch-wildcard-field
- https://docs.rs/tantivy/latest/tantivy/tokenizer/struct.NgramTokenizer.html
- Internal: 0aa42c30, `surveys/keyword.md`
