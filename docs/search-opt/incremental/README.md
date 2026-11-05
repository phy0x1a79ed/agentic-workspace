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
