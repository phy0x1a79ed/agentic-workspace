# Source plan: search, the federation layer

search (`search@1`) becomes kb's federation layer. It holds no content. It asks kb's own sources in-process and other nodes' kb over the hub.

How a query runs:
1. Parse the `query` grammar, unchanged, with its validation and error texts (`repr` quoting from awm-pycompat).
2. Choose the sources. A query that names none asks `journals`, the only default, as today.
3. Embed the query text once, if any named source takes vectors.
4. Ask each named source's live verbs in parallel, with the caller and the vector. Each source applies the marks and its own filters, then returns its sections. journals, guide and transcripts answer in-process. papers and library answer through the `papers` plugin's live verbs until their migration.
5. Each source has a per-source timeout. A source that is down, slow or failing returns an error in its own section, and the other sources still answer. No query waits past the timeout.
6. Peers: a source named `<src>@<node>` is asked through that node's search, as today. The hit cuts and the tag-clash refusal stay.
7. `get` routes to the source's live `get`, and keeps the usage row and score.

State: `search.db` keeps only judgements and the usage log. Its `sources` table and feed columns stay on disk, unread. guide's tables live in kb's `lance/guide/`.

Changes:
- Index mode goes, with the feed reads, cursors, epochs, layout keys, rebuilds and the hourly sweep. So do `reindex` and `api_export`.
- `sources` lists each source with `default`. `status` reports each source's last answer and error.
- search's `note_read` call passes `session`.
- The `journal`, `kb` and `transcripts` needs go. papers is a live source like any other, and an absent source is just absent.
