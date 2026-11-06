# sources

## Purpose & Contents

This file holds which sources search covers, how they relate to one mechanism, and where each one is stored.

## As built

- The signature names three kinds: `journals`, `kb` (scientific papers) and `transcripts`. Each carries a node, such as `kb@altair`.
- Journals live on each node in the journal database. search keeps its own SQLite index of them, rebuilt by an export pass.
- Papers live in kb on altair, with their own FTS5 and vector indexes. kb serves search only.
- Transcripts are a live source that search calls.
- Earlier, Tony: "papers can stay slow, daily use journals is what matters. I mean the same mechanism can prolly work for both" and "We can then use this to hold each of the sources separately."

## Decisions

- 2026-11-05: Search is one mechanism shared by every source. Each source keeps its own store on the node that owns it. Journals set the speed bar. Tony: "Yes".
- 2026-11-05: Adding a source is generic. A source declares its own fields, and the fields may change. No field outside the core is common to every source: papers have no natural timestamp, and journals have no DOI or author. Tony: "Adding sources needs to be generic since columns can change. Timestamp doesnt exactly fit science articles and things like doi or author doesnt work for awm  journals".
- 2026-11-05: A source holds entries. An entry is a list of fields. Each field has a name, such as `text`, `title`, `author` or `ts`, that a query refers to. Each field has a class: `content` is a keyword and semantic search target, `categorical` holds values such as tags, authors or a journal name, `numeric` holds numbers, and a date-time class is likely. This replaces a fixed core of `id`, `title` and `text`. Tony: "Id argue that each source has entries consisting of lists of fields. Each field has a name (like text or title, author, ts, etc) to be referenced in the query, and maybe a class/type from one of: content (keyword/semantic search target),  categorical (like tags, author, scientific journal name),  numeric, etc (date time?)".
- 2026-11-05: Order is not a property of a source. A source stores entries in whatever order the implementation needs. The query orders results by relevance, and may gain a sort. Tony: "Order is a search concern. I dont think this matters. Order however necessary and query will reorder by relavence whatever that means -> maybe need to add a sort by schema in querry".
- 2026-11-05: Four field classes. `content` is text that `$contains` and `$about` search, across all of an entry's content fields together. `categorical` holds one or more exact values. `numeric` holds numbers. `datetime` holds ISO-8601 values and is apart from `numeric` so a date alone can bound a range. A link such as `replies_to` or `cites` is a categorical field of entry ids. The id is not a field: the source assigns it to every entry. Tony: "I accept both".
- 2026-11-05: Each source marks which of its fields a hit shows. A hit always carries the id. This replaces the signature's fixed `{id, title, ts}`. Tony: "I accept both".
