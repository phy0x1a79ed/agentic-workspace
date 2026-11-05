# incremental

## Purpose & Contents

This file holds how search keeps each source's index current without reading the whole corpus: what a source supplies, and what the engine updates.

## As built

From 367a5d0d.

- Each pushed journal event runs a full export pass, about 109 pages at 10x. Only changed threads are rewritten and re-embedded, but each pass re-reads every stored embedding's hash.
- Journals are mutable: body edits, addendum folds, comments, retags, and tag renames that fan out to every thread with the tag. An id cursor misses these.
- An earlier proposal: a per-thread change sequence, bumped where the journal already indexes a post, plus a reconcile by id and hash and a per-chunk vector carry. Tony has not approved a change to the journal export contract.
- Vectors carry by a hash of the chunk text, so a re-embed covers only new text (82c9915c).

## Decisions

- 2026-11-05: Every source supplies a change feed as part of the generic source contract. A source keeps a sequence number that rises on every add, edit, retag or delete. It lists the entries changed since a given number, deletions included. Search stores the last number it read and asks only for what follows. Search never rescans a source. For journals the bump sits where the journal already indexes a post, so a tag rename bumps every thread it touches. This approves the change to the journal export contract. Tony: "This makes sense" and "I accept your feed source suggestions".
- 2026-11-05: Vector clustering is an optimization, not an accurate clustering. Look at DIAMOND linear clustering for anything to lift. Amortized balancing may recluster a subset of clusters chosen by a deterioration score. These are suggestions. Tony: "For clustering, it doesnt have to be accurate clustering in the data science sense, it just has to work as an optimization. Have a look at diamond linear clustering to see of anything can be lifted there. Also, we could do "amortized balancing" where we recluster a subset of clusters based on some deterioration score. Just suggestions here".
- 2026-11-05: Use LanceDB with its own incremental updates and partition maintenance for now. No own rebalancer. Tony: "Just using lance db plus its incremental updates is fine for now."

## Lance partition maintenance

Read from Lance `main` at 05a128c, `rust/lance/src/index/vector/builder.rs` and `rust/lance-index/src/lib.rs`.

- A partition splits when it holds more than 4 × `target_partition_size` rows. IVF_RQ's default target is 4,096, so the split threshold is 16,384. A partition below 25% of target joins its neighbours.
- A split trains new centroids from a sample of 256 rows per centroid. It then re-reads the raw vectors of every row in the split partition and in the 64 partitions nearest its old centroid (`REASSIGN_RANGE`), and reassigns them.
- **CAUTION** `REASSIGN_RANGE`, the split factor 4 and the join percentage 25 are compile-time constants. Changing them needs a patched Lance. `target_partition_size` is the only setting.
- A split reads only the vector column, not the text.
- With fewer than about 65 partitions, about 266k vectors at the default target, one split reads every vector. Above that it reads about 65 partitions' worth: about 27% at 1M and about 10% at 2.7M.
- New rows go to the nearest partition. A partition starts near 4,096 rows, so splits begin when a region of the corpus has grown about 4 times. Even growth means about one wave of splits per 4 times growth. A hot topic splits its own partition sooner.
