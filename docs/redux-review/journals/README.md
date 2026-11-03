# journals

## Purpose & Contents

This doc holds the review of the journal and of precedence: how they are built today, where the build and Tony's wishes disagree, and each decision Tony makes on them. Tony's wishes are comment threads, tags from a set ontology with editable descriptions, complaint posts such as bug reports, a closed state like an issue, and precedence folding into the journal. The settled decisions go into the `docs/redux` area docs as requirements.

## As built

Surveyed read only on mizar, 2026-11-03, against `origin/release` and the redux lanes.

- **Store.** `journal.db` holds `scope_posts(id, owner_project, owner_scope, author, kind, body, meta, ts)`, subscribers, read cursors and embeddings (`awm/services/journal/awm/journal/dao.py:22-66`). A scope is the channel. Messages, journals, system notices and goals are all rows of `scope_posts`.
- **Kinds are free strings.** The docs name message, journal, system and goal (`channel.py:16-21`). Nothing validates a kind (`operations/posts.py:163-170`). Search indexes only journal, message, goal, debrief and note (`search_index.py:25`).
- **No threads, tags or status.** A post has no parent field. The only links sit in goal `meta`: `supersedes` and `retires` (`goals.py:95-120`). Posts are append only, and goals use tombstones.
- **Verbs.** `post`, `fetch`, `subscribe`, `unsubscribe`, `inbox`, `ack`, the four goal verbs, `render`, `export` and `reindex`.
- **Precedence is its own store and plugin** (`precedence.db`, `install.md` R115).
  - A decision is a triple of context, question and decision, with status `active`, `superseded` or `retired`, `superseded_by`, votes and a seen count. It belongs to no project or scope.
  - `notes` attach one level of comments to a decision, with a validated kind: amendment, context change, supersede or comment.
  - `tags` are free text. A tag equal to a private project's name hides the decision from peers (`visibility.py:1-6`).
  - Search blends a usefulness score with cosine. The curation verbs (`edit`, `supersede`, `remove`, `merge`, `import`, `embed`, `export`) are CLI and HTTP only.
- **Redux lanes.**
  - `feat/redux-sessions` stamps the R9 session in `meta.session`, adds `meta.cites` and a `session_reads` table.
  - `feat/redux-trust` keeps edits as versions (`post_versions`), with `journal_edit`, `journal_history` and an addenda sweep (`fleet.md` R107 items 1 and 2).
  - Release, sessions and trust each define schema migration (1,2). They collide on merge.

## Differences from Tony's wishes

- **Threads.** No post has a parent. Precedence notes are the nearest model, one level deep.
- **Tags with an ontology.** The journal has no tags. Precedence tags are free text with no vocabulary and no descriptions. Kinds are free strings too.
- **Complaint posts.** No kind exists for them. Search would skip a new kind until it joins the indexed list.
- **Closed state.** Posts have no status. Precedence and goals each carry their own.
- **Precedence in the journal.** No requirement plans it. A fold carries the triple, votes, the usefulness rank, notes, the visibility rule and the curation verbs.
- **The docs.** No requirement in `docs/redux` covers threads, tags or a closed state. `fleet.md` R107 covers versions and addenda, and `scopes.md` R143 puts the brief next to the goal.

## Decisions

- 2026-11-03: A comment is a post with `reply_to`, nested like reddit, kept in the scope of the thread's first post. Its author gets each comment in the inbox. Search returns the first post with a comment count, and `get` returns the thread (`fleet.md` R107 item 5). Tony: "agree with proposed  comment layout".
- 2026-11-03: Tags come from one vocabulary the fleet shares and syncs. `tag_add` needs a description, and `tag_edit` changes the name or the description. A post links a tag by its id. Kinds fold into tags, and every kind filter reads tags (`fleet.md` R107 item 6). Tony: "tag edit also changes the name, so tags are linked to journals from some separate ID. changing name changes how the are rendered", "kinds get folded into tags -> existng filter/search functions now use multiclass tags" and "one synced vocab for whole fleet, yes".
