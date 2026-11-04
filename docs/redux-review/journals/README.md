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

## Multi-tag review

Reviewed read only on 2026-11-03, against `origin/release` 266f4b29 and the lanes, for R107 items 5 to 7.

- **Nothing blocks the change.** About 40 sites read or write `kind`. Each has a direct tag equivalent.
  - The journal: the `kind` column and index (`dao.py:49,55`), the post filter (`channel.py:284-286`), `INDEXED_KINDS` and export (`search_index.py:25`, `export.py:33,48-58`), goals and tombstones (`goals.py:39,166,174,234,248`), the inbox, render, and `posts.py` defaults.
  - Other services: the `posts` emit payload (`hub_adapter.py:66`), which `reporting/tasks.py:1136,1167` filters on. Also `reflection/alarm.py:100`, and search's `kind` filter (`search/query.py`, `index.py:92`, `store.py`, `federation.py:27`).
  - Outside the repo: the `debrief` skill posts `kind:"journal"` (`SKILL.md:53`). No hook names a kind.
- **The peer gate keys on kind.** `posts.py:217-225` lets a peer see only journal posts. If the gate keys on a tag anyone can change, adding `journal` to a message exposes it. `feat/redux-trust` replaces this gate with its `bounds`.
- **Live data on mizar.** 65 posts: 58 journal and 7 message. No other kind. altair holds most of the journal and needs the same count before the cutover.
- **Schema.** A `tags` table with id, name, description and origin node. A `tag_versions` table. A `post_tags(post_id, tag_id)` join. `scope_posts.reply_to`. Each kind maps to a fixed tag id. An unknown kind becomes a tag described as a migrated kind.
- **Migration numbers collide.** Release has (1,2) for cursors and sessions has (2,3) for reads. Trust redefines (1,2) for versions, so it never runs on a node already at v2. That is a bug on the trust lane today. The order is cursors, reads, versions, tags.
- **Compatibility.** Old peers, the `debrief` skill and the privacy probe send `kind`. The gateway refuses unknown arguments both ways. The journal accepts `kind` as an alias for one tag during the move, and returns a derived `kind`.
- **Lanes.** The sessions lane's cites and reads key on `kind='journal'`. The trust lane's addenda fold rewrites `kind` to `folded`, which loses the original kind. The `feat/svc-kb` search filters `kind`. The harness lane does not depend on `kind`.
- **Name clash.** `scopes.db` already has a `tags` table for project and scope tags. Search's privacy veil (`search/index.py:24-62`) hides any document with a tag equal to a private project's name.

## Decisions

- 2026-11-03: A comment is a post with `reply_to`, nested like reddit, kept in the scope of the thread's first post. Its author gets each comment in the inbox. Search returns the first post with a comment count, and `get` returns the thread (`fleet.md` R107 item 5). Tony: "agree with proposed  comment layout".
- 2026-11-03: Tags come from one vocabulary the fleet shares and syncs. `tag_add` needs a description, and `tag_edit` changes the name or the description. A post links a tag by its id. Kinds fold into tags, and every kind filter reads tags (`fleet.md` R107 item 6). Tony: "tag edit also changes the name, so tags are linked to journals from some separate ID. changing name changes how the are rendered", "kinds get folded into tags -> existng filter/search functions now use multiclass tags" and "one synced vocab for whole fleet, yes".
- 2026-11-03: Closed is a tag, with no `open` tag. Tag filters take negation and conjunction. One call adds and removes several tags on a post (`fleet.md` R107 item 7). Tony: "can we just make closed a tag? then we can do searches like !closed and complaint", "i want to avoid swapping open-closed. -> we also need a way to batch edit tags per journal".
- 2026-11-03: A superseding post replies to the old one, and the old post gets the `superseded` tag. Goals, briefs and precedents share this (`fleet.md` R107 item 8). Tony: "we can make supersedes by just reply links, then add superseded tag to old precedent entrty".
- 2026-11-03: Usage decays continuously with no sweep. Each item stores `s = ln Σ e^(λ·tᵢ)`, and the rank compares `s` directly. Steady use outranks a burst (`fleet.md` R136 item 6). The half-life is 30 days. Tony: "I want continuous decay, and continuous hits should outrank a burst in one day" and "30 days is fine".
- 2026-11-03: Precedence folds into the journal as posts tagged `precedent` at workspace level. Notes become comments, votes go for the usage rank, and the `precedence` plugin and tool go (`fleet.md` R107 item 9, `install.md` R115 item 4). Tony: "fold in precedence. we discussed this".
