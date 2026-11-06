# engine

## Purpose & Contents

This file holds the decisions made while building LanceDB into the `search` service, and the measurements behind them. `PLAN.md` holds the plan the build followed. `awm/services/search/INSTALL.md` holds the contract a source meets.

## Decisions

- 2026-11-05: Rank `$about` by weighted min-max fusion of the vector and keyword lists, as the old engine did. The vector score is one minus the cosine distance of the entry's best chunk. Alpha is 0.4 and each list is read to depth 100. The plan's reciprocal rank fusion lost 25 of the 99 sentence lookups at the same depth.
- 2026-11-05: Query the vector index with 20 probes and no refine pass. It is the cheapest pair that lost no answer in the replay.
- 2026-11-05: Store each source's sync state in `search.db` beside the judgements: the feed cursor, the epoch and a layout key. The layout key joins `ENGINE_VERSION`, a hash of the fields and hit, and the model name. A mismatch drops the source's tables and reads its feed from the start.
- 2026-11-05: Build the vector index only at 5000 chunks. Below that a flat scan is fast enough, and an IVF index needs enough rows to train its partitions.
- 2026-11-05: Cap the LanceDB session caches at 64 MB for indexes and 32 MB for metadata, so the service's memory stays flat as sources grow.
- 2026-11-05: Drop kb's `related` need. The journal ranking now runs in one engine, as the spec chose.

## Replay

Run on 2026-11-05 with the old and the new engine over the same snapshot of 2,169 journals. mizar holds no stored judgements, so the set is 2 asks with 6 known answers, 100 lookups by title and 99 lookups by a sentence from the entry.

| Questions | Old engine | New engine |
|---|---|---|
| Known answers, `$contains` | 3 of 6 | 4 of 6 |
| Known answers, `$about` | 3 of 6 | 4 of 6 |
| Title lookups | 100 of 100 | 100 of 100 |
| Sentence lookups | 99 of 99 | 99 of 99 |

The new engine lost no answer the old one found. Median latency fell from about 200–250 ms to about 30–70 ms. `scripts/replay.py grep` found 80 of 80 sampled phrases in `exact`, at 13 ms median.

## Rebuild cost

A full rebuild reads the journal feed from the start and embeds every chunk on the CPU. Each run below used a copy of the node's journal database, with the live services untouched.

| Node | Threads | Chunks | Rebuild | Peak memory |
|---|---|---|---|---|
| mizar | 107 | 705 | 25 s | 0.9 GB |
| altair, at `nice 10` | 2,161 | 16,371 | 276 s | 1.5 GB |

Model load is part of each time. The store took 72 MB on disk on altair. A read with no new changes took 20 ms. The journal's migration to its feed schema took 5.5 s on altair.
