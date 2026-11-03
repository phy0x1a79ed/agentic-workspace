# search

## Purpose & Contents

This doc holds the review of the search system: how it is built today, where the build and `docs/redux/fleet.md` R131 and R136 disagree, and each decision Tony makes on it. The settled decisions go into `fleet.md` as requirements.

## As built

Surveyed read only on 2026-11-03. The paths are on altair: awm `feat/svc-kb` at aefff432 and kb `kb/shards` at 3761b68.

- **Seven ranking stages.**
  - Papers, exact: literal matches, then all-words BM25 (`kb/fastlayer.py`).
  - Papers, related: BM25 and meaning matches over the body, abstract and title, fused by reciprocal rank (`kb/semantic.py`).
  - Papers, graph: neighbours ordered by the prior (`kb/hops.py`).
  - The other sources, exact: literal, then all-words BM25 (`search/index.py`).
  - The other sources, related: three fused lists. The archive's own embeddings keep their top hits at the head (`search/archive.py`, `merge.py`).
  - Fusion per source across this node, the live sources and the peers (`merge.py`).
  - A second PageRank reorders every source except papers, at half weight, by the calling scope (`archive.py`, `kb/graphrank.py`).
- **The prior.** It is PageRank over in-corpus citations, plus a 10-nearest-neighbour similarity graph, built offline. As a boost it gained nothing: 0.338 against 0.337 (`research/poc/RESULTS.md`). Paper ranking already skips it. It now orders only the graph section and breaks ties in hops.
- **Hops.** A hop runs personalized PageRank over the citation and author links, seeded by the top five hits and weighted by how clear the first hit is. recall@20 is 0.618 with the weighting and 0.569 for the first hit's authors alone. p95 is 152 ms. A text query cannot follow citations. The separate `kb hop` verb already exists, but it weights every seed equally.
- **The graph section.** It is the third section after exact and related. Only the paper source fills it, with papers that cite, are cited by, or share an author with the top paper hits.
- **Sources.** journals, precedence, notes and writing come from the index. kb papers, library and transcripts are live. Released search queries all but transcripts by default.
- **Journal gate (R136 item 4).** It passes as of aefff432: recall@10 0.721 against 0.689 for the released engine, and no released top-20 post is lost.
- **Opt in (R136 item 4).** A query with no `sources` returns an error that lists the sources. A query from an older peer keeps the old default.

## Differences from the design

- Cognee still serves graph and answer modes in kb. R136 item 3 keeps Cognee out. Its retirement waits on a scratch branch.
- The second PageRank shapes journal order (precedence 4ae555d5). R136 item 1 lets vectors only add to keyword results, and no measurement shows the PageRank helps.
- `fleet.md` § Gap says no code exists for the approved plan. T1 to T7 are done.

## Decisions

- 2026-11-03: Drop the prior and the graph section. Search returns exact and related. A hop is its own `kb hop` call on paper ids, and `then` goes (`fleet.md` R136 item 5). Tony: "accept drop both".
