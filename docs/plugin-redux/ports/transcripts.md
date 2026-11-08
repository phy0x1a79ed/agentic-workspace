# Source plan: transcripts

transcripts archives Claude Code session logs from `~/.claude/projects` into a gzipped mirror at `~/.claude/projects-archive`. It also restores and prunes them. In kb it serves the `transcripts` tool and the opt-in search source `transcripts`. The Rust module exists already: 29 tests, and a dual run against the Python that is clean in both directions (commit a2bbfe68).

Keep exactly:
- the archive layout: `.gz` per file, carried mtimes, and the `memory/` guard
- `transcripts.db` at v2
- peer rules: `sweep` and `prune` refuse a peer, and the read verbs hide marked slugs and sessions that entered a marked path, through the SDK privacy module
- `search` regexes with Python semantics through `fancy-regex`, with 400-character lines
- `sweep` fails closed when `harness-cc live_session_ids` cannot answer

Changes:
- The systemd timer becomes an in-plugin daily task. It runs at about 03:30 local time, catches up after 26 h, runs at idle IO priority, and records `trigger = "timer"`.
- **CAUTION** The sweep deletes the live file after archiving it. The Rust verifies each gzip (decompress and compare length and content hash with the source) before it deletes the source. A failed check keeps the source and counts a failure.
- New archive files are 0600 in 0700 directories, matching the live files. Today the archive is 0664 in 0775, which is world-readable. After statecheck's inventory, a listed deploy step tightens the existing archive to the same modes: 0600 files and 0700 directories. It changes modes only, so nothing is lost.
- **One search system.** The message text of every archived session goes into the transcripts LanceDB with a full-text index and no vectors. The live tree stays out, because `search` and `query` cover the archive only, as today. `query` answers from the full-text index, then keeps the lines that hold the words as typed. `search` scans the table and applies the regex with `fancy-regex`. The file walk and gzip reads leave the search path. **CAUTION** Matching runs on the message text, not the raw JSON line, so a pattern that matched JSON keys or ids no longer does.
- The index follows the archive on events. Only the plugin's own `sweep`, `restore` and `prune` change the archive, so each updates the rows of the sessions it moves or deletes. A start reconciles each archived file's mtime and size against its rows.
- Embedding is deferred. Measured costs: the conversation text alone takes about 5 h on capella, 5 h on mizar and 9 h on altair. Tool input and output add about 2.5 to 3 days per node.
- The unused `awm-claudedaemon` dependency goes.
- No needs.

Tests: port the 34 tests. Check that Python and Rust each read the other's gzip files. The dual-run sweeps, restores and prunes a 20-session fixture tree in two temp homes and diffs the trees and the DB.

Risk: capella has not swept since September, so its first timer sweep moves a backlog. Run `sweep {dry_run:true}` on each node before the land.
