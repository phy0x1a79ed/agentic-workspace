# Port plan: scopes

scopes runs the scope lifecycle on git, claims and queues, tags, the private marks, the project index and the hygiene pass.

Keep exactly:
- **The `git` binary**, about 45 invocations. libgit2 and gix run no hooks or merge drivers, lack worktree porcelain, and cannot use the `gh` credential helper. Set `LC_ALL=C` where output is parsed.
- **Per-verb timeouts:** create 1800, heal 1800, delete 600, and gather and scatter 900.
- **Inputs and errors:** Python truthiness in argument coercion, the error classes and texts (`seed.py` matches `already exists`), and pydantic key order through `preserve_order`.
- **Result shapes:** those of `claim`, `claims`, `resolve`, gather and scatter, `search` and `ScopeActionResponse`, which reflection, journal, cch and skills parse.
- **`scopes.db` at v5, unchanged.** The legacy `scope_posts` and `scope_subscribers` stay. They are not empty everywhere: capella holds 2,362 `scope_posts` rows and altair 804.
- The vagrant code stops reading `services/config/config.db`. capella holds one, and it is never deleted.

Changes:
- **scopes keeps no vectors.** `search` asks journal's `journal_query` for brief posts, maps each hit to its scope, and ranks a project by its scopes' hits. Exact name matches from scopes.db come first. The result shape stays. `reindex`, the `ServiceIndex` worker and its 6-hourly backfill go. The `embeddings*` tables stay on disk, unread: capella's 14,213 and altair's 10,001 rows include other types. The empty-brief defect goes with them.
- A per-project mutex guards git-mutating verbs.
- The vagrant bootstrap and `config_service` go as dead code. `search_eval` moves to awm-embed. The DVC drift check moves to awm-dvcdata (ops).
- The `journal`, `operations` and `reflection` needs go, because each degrades by design.

The create hook, for social's seats:
- A project opts in through `awm-project.toml` at the root of its main checkout, the worktree of the bare repo's HEAD branch. The file sits outside `.awm/`, which R142's sweep empties.
- `[scope] create_hook` names the script, relative to the checkout. The optional `create_hook_prefix` limits it to the scopes under that prefix.
- `create {project, scope}` on a matching scope runs the hook with the prefix stripped (`seats/ana` → `new-seat.sh ana`).
  - It runs in the checkout, as the scopes user, with stdin closed, under the cancel-aware runner.
  - The path must resolve to an executable inside the checkout.
  - The name checks and the "already exists" check run first. Git-only params are refused.
- Success needs three things: exit 0, a directory at `projects/<p>/<scope>`, and an active row that the hook's own `scope ensure` wrote. scopes then sets the brief and tags. A failure names the cause and the hook's last 20 output lines, and removes nothing.
- Hygiene skips hook projects. `delete` and the other verbs keep their worktree guards. Seats need no delete path yet.
- A project without the file behaves exactly as today.

Tests: port the 22 test files. The golden replay covers 8 call groups, peers included. The dual-run takes 14 write steps on two fixture workspaces with pinned commit dates, and diffs results, outbound calls, refs, worktrees and `.dump`. Add hook tests: argv, the prefix, the refused params, failure, a missing row, a path escape, no file, and hygiene.
