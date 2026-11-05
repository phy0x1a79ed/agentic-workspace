# spec

## Purpose & Contents

This file holds the specification of disk-based search for agentic memory: what it serves and the bounds it must meet. Each child topic settles one part: `sources`, `memory`, `latency` and `matching`. Design and implementation come after the spec settles.

## Input: the signature

The `search` signature is decided. It is in `docs/release-review/search-kb/search-signature/README.md` on `feat/redux-dev`. The spec must serve it as written.

- `search` takes `query` and `max`. `max` caps hits per source per section, default 10.
- `query` is a string or an object. `text` holds the text condition. Every other key is a source, `kind[@node]`: `journals`, `kb`, `transcripts`.
- `text` is a string, `{"$contains": s}`, `{"$about": s}`, or `$and`/`$or` over those. `$contains` is a case-insensitive substring, as grep does. `$about` ranks by meaning, blended with any-word BM25. A bare string means `$about` or `$contains`.
- Each source takes a MongoDB-style filter over its own fields, such as `project`, `scope`, `ts`, `tag`, `replies_to`, `author`, `cites`.
- Each source answers in `exact` and `related`. Without `$about`, a section lists newest first.
- A hit is `{id, title, ts}`. `get` opens one.

## Rough spec

Tony, 2026-11-05: "Basic rough spec is disk based search over text, mainly journals, for agentic memory. Other sources will be added, like transcripts and scientific papers. Want disk because keeping a large corpus in ram is not feasible with essentially unlimited size. But something like 100mb is acceptable. It needs to be fast because agents will constantly call it so something like 1s is prolly max, but flexible. Simple queries like last journal or journals about x where x is specific/obvious should return fast like 0.1s. Search requires both exact and semantic matches."

## Decisions

- 2026-11-05: Search is a fuzzy lookup over agentic memory that answers "did we do this before, and if so, how?". It centres on journals on purpose. Papers and other sources use the same mechanism but set no requirement. Tony: "We are really building something closer to "fuzzy lookup" that answers "did we do this before, and if so, how?". This is centered around journals and that's intentional. Papers and other sources are just along for the ride."
