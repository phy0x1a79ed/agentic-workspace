# matching

## Purpose & Contents

This file holds what search matches: exact matches, semantic matches, and the queries it is built for.

## As built

- The signature's `$contains` is a case-insensitive substring of a content field, as grep does. `$about` ranks by meaning, blended with any-word BM25. A bare string means `$about` or `$contains`.
- Today's ranked queries run BM25 over passages and a dense leg over chunk vectors, fused by reciprocal rank.

## Decisions

- 2026-11-05: Search serves specific lookups where the caller knows a few keywords. Semantic matching covers paraphrases, not hard cases with poor matches. Search need not handle a query whose words share nothing with the answer. Tony: "I think we dont need to consider semantic cases with bad matches. I think most cases are targetting pretty specific cases where a few keywords are known. Semantic matches i expect are for paraphrases rather than hard cases."
- 2026-11-05: The signature already settles matching. An exact hit is the string exactly as written between its quotes, case-insensitive: `$contains` as specified. No word splitting, reordering or word-form tolerance. A semantic target is one vector per quoted string. Tony: "This is spec'd out. Exact hits are just that, exaclty as written from quote to quote (but case insensitive). Semantic targets are treated as one vector per entry (quote to quote) We dont want to do additional gap coercion at this stage."
