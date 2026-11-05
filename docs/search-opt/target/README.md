# target

## Purpose & Contents

This file holds the target for disk-based search: which corpus, which node, cold or warm, and what latency counts as done. It also holds whether the journal export contract may change to reach that target.

## As built

Measured by the previous session on altair (journal 367a5d0d). The harness and data are at `altair:~/so_x10`.

- **Corpus.** 10x altair's journal export: 21,690 docs, 172,560 passages and 200,890 chunk vectors in a 1.46 GB SQLite file.
- **Warm.** p95 2.66 s, max 3.07 s, RSS 1.9 GB. This passes the old gate of p95 <= 10 s and RSS <= 4 GB. The dense leg reads all 201k vectors from SQLite, a median of 1.3 s.
- **Cold.** The page cache is evicted. Each ranked query ran past 120 s. The dense leg took 57-170 s and chunk BM25 took up to 380 s.
- **Disks.** altair's disk does about 17 random 4 KB reads/s at queue depth 1. mizar's SSD does 3,682. altair has 58 GB of RAM and mizar has 15 GB.
- **Sync.** Each pushed journal event runs a full export pass. Journals are mutable, so an id cursor misses edits, retags and tag renames.

## Decisions

