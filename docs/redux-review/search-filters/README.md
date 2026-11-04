# search-filters

## Purpose

This file holds the result of the review topic on how a caller filters `search`: which sources and nodes it covers, the tag filter, and the structured query that replaces `kb hop`.

## As built

- `search query` takes `sources` (required: `name` for this node, `name@node` for another), `tags` (the journal tag grammar, with `and` and `!`), `hop` (a JSON object or text: `via`, `from`, `limit`), `project`, `scope`, `after`, `before` and `limit`. `query` is optional when `hop` has `from` (search 538ae158, 06e0597c).
- No `peers` argument, no `kind`, and no precedence source. Precedent posts are journal posts tagged `precedent`.
- `hop` walks `citations`, `cited_by` and `authors` in kb, and `reply_to` in the index sources. kb publishes no MCP tool.

## Decisions

- 2026-11-04: Each source carries its node, such as `kb@altair`. A bare source is this node's, and no source named means this node only. The `peers` argument goes. Tags replace `kind`, and precedent posts replace the `precedence` source (`fleet.md` R136 item 7). Tony: "A whitelist is fine. Each entry needs to specify peer. If no peers are specified, then it is just local only." and "Kind needs to be fixed. Precedence needs to be fixed."
- 2026-11-04: `sources` is required and has no default. A bare `journals` means this node's journal, the common case (`fleet.md` R136 item 7). Tony: "Let's not have a default for the sources. But I anticipate that the majority of these will just be search journal, implying local journal."
- 2026-11-04: An optional `hop` JSON object walks links from the hits, and an empty one walks nothing. `query` becomes optional. The `kb` tool goes, so kb serves search only. No argument takes raw SQL. The shape is a first try (`fleet.md` R136 item 8, `install.md` R134). Tony: "The SQL query should be optional, and so it just does no additional hops if empty. As for the format, I think we should do maybe a JSON object. This part isn't really flushed out, I'd say. It's not really been used before, so we'll just have to pick an implementation and try. I don't want to do raw SQL though, because I think that has like risk of dropping tables."
