# keyword: survey of disk-based keyword and string search

## Purpose & Contents

This file holds the read-only survey of disk-based keyword, phrase, prefix and substring methods, judged by cold-cache reads on altair's disk and by update cost. It feeds the **keyword** topic. It is not a decision.

## Cost model

altair costs about 60 ms per random read at queue depth 1 and about 3.5 ms at queue depth 32. A 64 KB read costs about the same as a 4 KB read. A 1-2 s cold budget therefore affords 15-30 serial seeks. Count seeks, not bytes.

## Methods

| Method | Cold reads per query | Cold | Update cost | Py/Rust |
|---|---|---|---|---|
| FTS5 `ORDER BY rank` | Every term's full doclist: 47-62 MB at 99k passages (900d3eab) | Up to 380 s today (367a5d0d) | Cheap in place | Both |
| FTS5 bare `LIMIT` | About 40 KB. Stops early, in rowid order (900d3eab) | Good, unranked | Same | Both |
| FTS5 64 KB pages + `optimize` + VACUUM | Each doclist is one contiguous run | A cold 3M-posting read fell from 66 s to 1.2 s (0aa42c30) | VACUUM rewrites the file | Both |
| FTS5 detail=column or none | Index 3-5 times smaller | Fewer pages | Same | Loses phrase and NEAR |
| FTS5 trigram | Candidate lists, then a body read per candidate | Common trigrams are huge and need `TRIGRAM_BUDGET` (0aa42c30) | Same | Both |
| Champion lists (each term's top r) | About one run per term. OR-leg reads cut about 1,000 times (900d3eab) | Good | Batch rebuild plus a small exact delta index | Own build, on FTS5, in kb |
| Prior-ordered docids + early stop | The head of each list | Good when the prior matches intent | A prior change means delete and reinsert | FTS5 rowid order, Lucene index sorting |
| Impact-ordered lists (JASS) | List prefixes, any-time stop | Good | Static | Research code |
| Tantivy | Term dict + skip blocks + postings + a doc-store block per hit | Fair. mmap faults are QD1 random reads | Immutable segments | Rust, tantivy-py |
| Quickwit hotcache + batched ranges | About 2 round trips | Built for high-latency storage | Batch | Rust |
| Lucene family | Like Tantivy | Same | Segments | JVM |
| Xapian | B-tree descent + chunked postings | Fair | True in-place replace | No pip wheel, no Rust |
| PISA | Fewest postings decoded | Built for warm RAM | Static | C++, pyterrier-pisa |
| Sequential scan of a compact index | The whole file in order | Fine while small. Unmeasured | Rewrite | Any |

## Conclusions

- Each term's postings must be one contiguous run, read in parallel at QD32. Engine-level pruning such as block-max WAND still does random skip reads on a cold mmap.
- Storage order is the early-termination prior. For journals, recency is a natural prior that is stable under edits and retags. A bare `LIMIT` over ts-ordered rowids returns the newest matches from list heads.
- Ranked relevance can come from champion lists refreshed in batches, plus an exact FTS5 delta over posts changed since the last build. svc-kb built and tested this for papers (900d3eab, 761c2517).
- Staying on SQLite FTS5 fits. It updates in place and every needed fix is measured internally. Tantivy is the Rust option if an engine change is ever wanted.
- Hits return `{id, title, ts}`, so keep titles in a small table of their own and fetch them last in one parallel batch.
- Prefix costs one lookup with FTS5 `prefix=`. Substring on journals may be cheapest as a sequential scan of a compact bodies table. Keep trigrams with a budget for papers only.

## Measure next

- altair's sequential MB/s. 5f2c38e3 measured 17 MB/s.
- The size of the 10x FTS5 index with detail=column against detail=full.
- The 64 KB-page cold rerun (`x10_64k_*.jsonl` on altair).
