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
- 2026-11-03: A hit returns its title only, and a `get` on it is the usage feedback for the scope-tuned rank. The rank switches on when the journal replay shows a gain (`fleet.md` R136 item 6). Tony: "maybe this can be natural? search returns titles only and agent expanding full entry is the feedback?", "title ok" and "I dont need search updated immediately".
- 2026-11-03: Cognee retirement re-lands, because R136 item 3 already keeps Cognee out. It was held only by the ranking freeze.
- 2026-11-03: The cut line for Cognee in kb stands as the search-finish job proposed. `kb_recall`, the `/recall` route, `recall.py`, `engine.py`, the Cognee and Ladybug builders and the cognee pin go. `query`, `library`, `get`, `hop`, `related`, `/encode`, `/related`, the fast layer, the hop arrays and `graph.db` stay. The plain hybrid and lexical modes go too (`fleet.md` R136 item 3). Tony: "agree with drop lexical modes".
- 2026-11-03: One usage rank serves the whole node and updates itself on each `get`, with no rebuild step. The `kb rank` code stays as its engine (`fleet.md` R136 item 6). Tony: "lets make page rank by project rather than scope" and then "lets actually just make kb rank automatic across a whole node. then no rebuild".
- 2026-11-03: The usage rank is a stored score per item, updated on each `get`, not a PageRank walk. It orders only the first ranked search. A walk over links belongs to the SQL-style query, and the `kb rank` PageRank goes. The score is read in order from disk (`fleet.md` R136 item 6). This replaces "the `kb rank` code stays as its engine". Tony: "lets drop graphrank -> this is part of sql style query. page rank only for initial search" and "on dropping graphrank that is correct. I want to make sure that the original objective of using pagerank, which lets us search through numerous entries from a disk only source is held."

## Build result

Reported by search-finish on altair, 2026-11-03. Feature branches only. dev and release are unchanged.

- **Journal gate after R136 items 5 and 6.** recall@10 is 0.721 and recall@20 is 0.778 on the 54 judged queries. The released engine scores 0.689 and 0.776. No post is lost.
- **Commits.** awm 5cf88c81 drops `then`, the graph section and the PageRank reorder. awm 19ffd55e and kb a4a2dab move the seed weighting into `kb hop` and remove the prior. awm 7fc61fe9 makes hits title only and logs each `get` to a `usage` table that no rank reads.
- **Choices the lane made.** `kb_get` returns a whole paper, so `search get` can open a title-only paper hit. `context` and `passages` stay declared and do nothing, so open sessions and older nodes keep working.
- **Criterion 5 passes.** Reported by altair job 6a450f72 on 2026-11-03. Over 300 more-by-these-authors cases, a query followed by `kb hop` scores recall@20 0.878 on 20 results. The old `then` path returned at most 10 papers and scored 0.634. The new path's top 10 scores 0.646: 268 cases are the same, 30 better and 2 worse. p50 is 5.7 s and p95 is 8.8 s.
- **The sharpening step stays in `kb hop`.** Without it the score fell to 0.592 (awm d4cfc124).
- **Not run.** The full replay and grep parity stopped, because the journal gate numbers already stand.
- **Cognee is out of kb (N3).** Reported by job 6a450f72 on 2026-11-03, both branches pushed.
  - kb `feat/shards` e997f9b: the kb server answers `/encode`, `/related` and `/status` only. `graph.py` keeps `graph.db` and its PageRank for `kb rank`. The nightly entry refresh and OpenAlex steps still run.
  - awm `feat/svc-kb` 4857c0f0: `kb recall` and `kb snapshot` go. The get log records project and scope in their own columns (schema v6). No rank reads it yet.
  - Tests pass: kb repo 75, awm search 163, kb 72, journal 157.
- **Docs follow (N2).** kb `feat/shards` 6a7e403 and awm `feat/svc-kb` b2a6d070 describe kb without Cognee, recall or ingest. The search INSTALL describes title-only hits, the `get` log and the rank kept off until the replay.
- **The `kb rank` PageRank is gone.** kb `feat/shards` 3d1c391: `graph.py` keeps nodes and edges only, and `kb.hops` still reads the author and citation edges. awm `feat/svc-kb` aab968bd: the `kb_rank` verb and `graphrank.py` go. The kb and search suites pass. `graph.db` still ships, though awm no longer reads it. It waits on the search-filters topic.
