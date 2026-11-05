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

