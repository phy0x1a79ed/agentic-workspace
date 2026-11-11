# Port plan: kb

kb is `core/plugins/kb`, crate `awm-plugin-kb`, binary `kb`. It replaces the journal, search and transcripts plugins and serves their tools, `journal`, `search` and `transcripts`, with the same verbs. The describe of each tool equals its baseline, apart from the changes listed under Acceptance criteria.

Shape:
- **Sources.** Each source is a module behind one trait: its name, `default`, `query` with the caller and an optional vector, and `get`. A source applies the marks for its caller and returns its sections with `veiled: true`.
- **One LanceDB per source** under `<AWM_DIR>/services/kb/lance/<source>/`, each in yearly sections through awm-index. A source that grows too large gets a new section, never a second engine.
- **State in place.** kb opens each old plugin's state dir where it is: `services/journal/journal.db`, `services/search/search.db` and `services/transcripts/transcripts.db`. Nothing moves on a node that runs these plugins as the user today.
- **One shared embedder.** awm-embed loads MiniLM once in kb. journals and guide embed their writes. transcripts and the query path of a source with no vectors skip it.
- **Where kb runs.** kb runs on every node as the user, like the other plugins. A node holds the databases whose data it has: transcripts on every node with `~/.claude`, journals where journal runs today, guide everywhere, papers on altair alone.
- **The singleton journal goes.** Where journal runs today as the `awm-journal` unit (`/var/lib/awm/journal`), the cloud playbook `homelab/journal_return.yml` stops the unit, copies `journal.db` with SQLite's backup API into `<AWM_DIR>/services/journal/`, checks the post count, and keeps the original as a root-owned rollback copy. It then removes the unit, its token and the `awm-journal` user. It runs on each node before kb's land.
- **papers is a kb source.** It ranks by bge-large, which it loads itself, and keeps semdb's vectors. The Python papers plugin, its `kb` tool and its `library` source are gone.

Gotchas: one binary carries LanceDB (about 238 MB), so its pin and build time grow once, not per plugin. The Rust kb serves no tool named `kb`.
