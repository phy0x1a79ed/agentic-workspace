# search-filters

## Purpose

This file holds the result of the review topic on how a caller filters `search`: which sources and nodes it covers, the tag filter, and the structured query that replaces `kb hop`.

## As built

- `search query` takes `query` (required words), `sources` (a whitelist of names), `project`, `scope`, one `kind`, `after`, `before`, `limit` and `peers` (this node by default, `fleet` or node names). altair served this on 2026-11-04.
- The sources on altair are journals, kb, notes, precedence, writing and the opt-in transcripts.
- A walk over citation and author links is only `kb hop`, on paper ids (`fleet.md` R136 item 5). No search argument follows links.

## Decisions

- 2026-11-04: Each source carries its node, such as `kb@altair`. A bare source is this node's, and no source named means this node only. The `peers` argument goes. Tags replace `kind`, and precedent posts replace the `precedence` source (`fleet.md` R136 item 7). Tony: "A whitelist is fine. Each entry needs to specify peer. If no peers are specified, then it is just local only." and "Kind needs to be fixed. Precedence needs to be fixed."
