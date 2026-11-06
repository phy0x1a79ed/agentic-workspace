# latency

## Purpose & Contents

This file holds the latency targets for search: how fast each kind of query returns, under which conditions, and how latency relates to corpus size.

## As built

- Measured on altair's journals at 10x (367a5d0d). Warm: p95 2.66 s. Filter-only and `$contains` queries took about 10 ms. Ranked queries took about 2 s.
- Cold, with the OS page cache evicted: ranked queries ran past 120 s. Every ranked query reads every vector and every matching posting, so its cost grows with the corpus.

## Decisions

- 2026-11-05: Latency is a soft target. Search runs constantly, so it must be fast and reliable for every query, old entries included. Caching is an implementation detail, and a design that is fast only on cached recent entries fails: a lookup into old material is where search helps most. A result in about 2 s is fine. 10-30 s is acceptable as a one-off, never as the norm. Over a minute is out. A query's cost therefore must not grow with the age or size of the corpus. Tony: "Caching is an implementation detail and maybe a trap. If only latest journals are cached and search is "normally" fast but grows to be minutes to hours on a search to old stuff where search is meant to be the biggest lift, then that's prolly bad design. Search speed is also a soft target. All im saying is that search will be used constantly so it needs to be reliable and fast. How fast exactly is fast? Idk. Prolly less than a minute, but a search that comes back in 2s, prolly ok. 10? 30? One offs maybe, the norm? -> no".
