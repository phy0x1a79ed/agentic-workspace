# memory

## Purpose & Contents

This file holds the memory bound on search: what the 100 MB covers, and how the corpus relates to RAM.

## As built

- Measured on altair's journals (367a5d0d). The search process holds 825 MB resident at 1x and 1.9 GB at 10x.
- Warm latency relies on the OS page cache holding the index. With the page cache evicted, ranked queries ran past 120 s.

## Decisions

