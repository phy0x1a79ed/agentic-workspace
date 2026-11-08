# Port plan: kb

kb serves papers to search as the live sources `kb` and `library`, plus `get`, the hop arrays and the encoder child. It keeps its 10 verbs, all on cli and http only.

Keep exactly:
- The fast layer, from SQLite alone:
  - stems from SQLite's own `porter unicode61` through `fts5vocab`, which bundled rusqlite gives for free
  - the trigram literal leg with its budgets, the AND and OR legs, and champion lists on schema 4
  - the 12 lookup threads and 32 reads in flight that altair's disk needs
- Hops: `.npy` arrays read by `memmap2` with a small header parser, personalized PageRank, and the surname filter by a hand-ported difflib `ratio`.
- The 5 s shared answer between `query` and `library`, the error texts, and the operator-only and local-only refusals.
- `indexmap` wherever Python iterates a dict, so float sums and ties match.
- `papers.db` stays read-only. The delta keeps its unversioned DDL. The library copy keeps its sha1 signature.

Changes: zotero is called with no `peer` unless `KB_ZOTERO_PEER` is set, which fixes the 400s, and an absent zotero logs once per state change. No needs.

Tests: port the 63 tests with fixture bases built in Rust. The golden replay runs on altair (real base) and mizar (empty shapes). The dual-run takes one zotero page set through `take_items`, `remap` and `library_copy`. Keep the slowest query within 1.2 times the Python's time.
