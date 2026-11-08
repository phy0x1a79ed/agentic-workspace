# Source plan: journals

journal holds scope channels, tags with cross-node sync, briefs, precedents, versions, inbox cursors, lineage, and its own search engine for search.

Keep exactly:
- **Caller rules**, from the SDK `Call`:
  - `trusted` is unbounded. Do not call `journal_bound` for it.
  - cooperative and untrusted callers need their own scope.
  - A peer cannot use `reads`, `inbox`, `ack` or the lineage verbs. A peer's post stores its `as` as author.
  - The clash gate refuses `fetch` and `journal_query`.
- **Privacy:** journal reads the marks itself, because the gateway filters a peer's args and emits but never a result.
- **Byte formats:**
  - `ts` as Python `isoformat`
  - `meta` as `json.dumps`, in key order
  - stored diffs from difflib `unified_diff`
  - `render`'s `content`, byte for byte, because every session's startup reads it
- **journal.db at schema v6.** The migration loop stays for nodes below 6. The dead `embeddings*` tables stay untouched.
- The tag pull every 6 h, the push on each tag change, the `posts` emitter, and the result shapes reflection, cch, scopes and search parse.

Storage, split with no copy:
- **LanceDB** (`lance/`, through awm-index) holds the posts: id, project, scope, author, ts, title, body, `meta`, tag ids and citations as list columns, reply and thread links, and version. It also holds their chunks, in yearly sections.
- **journal.db** holds the relational state: the tag registry, tag history and clashes, edit history (`post_versions`), inbox cursors, read receipts and session lineage. Its rows refer to posts by id only.
- **Frozen tables.** `scope_posts`, `post_tags`, `post_cites` and the `search_feed*` tables stay in journal.db, read-only and never dropped in this plan. They are the pre-redux record and the source of the cutover copy.
- **Reads.** fetch, render, inbox and threads query Lance. Where a read needs relational state, journal joins in code. For example, inbox is the posts after the cursor, minus the read receipts.

Write order, so a crash never loses a record:
- **A post** is one Lance row, with its tags and citations in that row, so it commits atomically. Its chunks follow.
- **An edit** writes the `post_versions` row in journal.db first, then updates the Lance row.
- **A retag** rewrites only the Lance row. A tag rename changes only the registry, because posts hold tag ids.
- **A delete** removes the Lance row. Read receipts that name it are harmless and stay.
- **Startup reconcile.**
  - A post whose newest `post_versions` row is ahead of its Lance version gets that body applied.
  - A post with missing or stale chunks gets them rebuilt.
  - Every Lance write is `merge_insert` on id, so a reconcile or a replay never duplicates a post.

Peers: a peer's post arrives through the same `post` path as a local one. Tag sync touches only the registry.

Rollback: revert the code, then run the export-back tool (a `journal` CLI subcommand). It copies every Lance post dated after the cutover into the frozen tables, through `merge_insert`-style upserts on id. T14 tests it on a snapshot.

Changes:
- `session_of` through reflection goes, replaced by the stamped caller session.
- `note_read` gains an optional `session`, honoured only from a service caller, so search's read records work again.
- `tag_list` and the tag push omit, for a peer, each tag the marks hide: a tag named for a marked project, and a tag used only on posts in marked channels. The Python has no such filter, so peers would see these once sync goes live.
- journal refuses to create a fresh DB when a `journal.db` exists at another known path on the node (`/var/lib/awm/journal/services/journal/`, `services/journal.moved/`), and names that path. mizar's empty `services/journal/` must not become a second journal.
- The `scope`, `search@1` and `reflection` needs go, because every call fails soft.
- journals answers search in-process inside kb. The `journal_query` and `journal_get` verbs, off MCP, serve scopes and other callers over the hub. Each answer applies the marks for the caller and returns `veiled: true`. Its archive entry becomes `mode = "live"`. `export` and the `search_feed` triggers go.

Tests: port the 163 tests. Add the write-order crash cases (kill between the two writes of an edit, and before the chunks), the reconcile, a peer replay of `journal_query`, a section boundary and the export-back. The golden replay covers 17 call groups, from a plain shell so `fetch` records no reads. The dual-run takes 13 write sequences on snapshots of mizar's and capella's DBs. It diffs the replies, the relational `.dump`, and the posts: Python's `scope_posts` against Rust's Lance rows, exported to the same shape. Ids and now-times are normalised.
