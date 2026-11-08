# Port plan: transcripts

transcripts archives Claude Code session logs from `~/.claude/projects` into a gzipped mirror at `~/.claude/projects-archive`. It also restores and prunes them, and serves the opt-in `live` source `transcripts`.

Keep exactly:
- the archive layout: `.gz` per file, carried mtimes, and the `memory/` guard
- `transcripts.db` at v2
- peer rules: `sweep` and `prune` refuse a peer, and the read verbs hide marked slugs and sessions that entered a marked path, through the SDK privacy module
- `search` regexes through `fancy-regex`, with 400-character lines
- `sweep` fails closed when `harness-cc live_session_ids` cannot answer

Changes:
- The systemd timer becomes an in-plugin daily task. It runs at about 03:30 local time, catches up after 26 h, runs at idle IO priority, and records `trigger = "timer"`.
- **CAUTION** The sweep deletes the live file after archiving it. The Rust verifies each gzip (decompress and compare length and content hash with the source) before it deletes the source. A failed check keeps the source and counts a failure.
- New archive files are 0600 in 0700 directories, matching the live files. Today the archive is 0664 in 0775, which is world-readable. After statecheck's inventory, a listed deploy step tightens the existing archive to the same modes: 0600 files and 0700 directories. It changes modes only, so nothing is lost.
- The unused `awm-claudedaemon` dependency goes.
- No needs.

Tests: port the 34 tests. Check that Python and Rust each read the other's gzip files. The dual-run sweeps, restores and prunes a 20-session fixture tree in two temp homes and diffs the trees and the DB.

Risk: capella has not swept since September, so its first timer sweep moves a backlog. Run `sweep {dry_run:true}` on each node before the land.
