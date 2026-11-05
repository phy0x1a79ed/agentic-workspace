# latency

## Purpose & Contents

This file holds the latency targets for search: how fast each kind of query returns, under which conditions, and how latency relates to corpus size.

## As built

- Measured on altair's journals at 10x (367a5d0d). Warm: p95 2.66 s. Filter-only and `$contains` queries took about 10 ms. Ranked queries took about 2 s.
- Cold, with the OS page cache evicted: ranked queries ran past 120 s. Every ranked query reads every vector and every matching posting, so its cost grows with the corpus.

## Decisions

