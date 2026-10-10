# Workspace Protocols

## Purpose & Contents

This file holds the procedures for operating the workspace. Read it by path when a task needs one. It covers the workspace layout, the scope lifecycle, scope naming, hub integration, per-user and shared data, data and backups, retiring a project, pushing to a peer, submodules, and the CLI.

Agent orientation goes in `AGENTS.md`. awm internals go in `ARCHITECTURE.md`. Cross-node behaviour goes in `FEDERATION.md`. What a project is for goes in that project's own files.

Nothing enumerable goes here, with one exception: a table that records a decision this file makes, such as the scope-prefix families or the hub roster. A snapshot of state does not qualify.

## Workspace layout

| Path | Purpose |
|------|---------|
| `AGENTS.md` | Agent orientation, loaded into every session |
| `PROTOCOLS.md` | This file |
| `ARCHITECTURE.md` | awm internals |
| `README.md` | Human setup and usage |
| `awm/` | The awm package tree |
| `skills/` | Reference procedures, read-only |
| `data/` | The shared DVC cache and legacy per-project data |
| `projects/` | Project bare repos and their scope worktrees |
| `tasks/` | DAG node execution sandboxes, gitignored |
| `.awm/` | Workspace runtime state |
| `.mcp.json` | The canonical MCP server registry |

`skills/` is reference-only. The skills service is retired. The end-of-session `debrief` is a native Claude Code skill.

## Scope lifecycle

1. **Create.** `scope(verb="create")` makes a git worktree on `feat/<scope>` with `.awm/` metadata.
2. **Start.** The agent runs the startup ritual in `AGENTS.md`.
3. **Work.** Code goes in the worktree. Data goes in `data/`.
4. **Debrief.** The agent runs the `debrief` skill. It posts the session trace as `kind=journal`.
5. **Complete.** `scope(verb="complete")` updates the status and optionally merges the branch.

`awm scope heal` repairs scopes. It is idempotent. Run it with `--dry-run` first to preview. It keeps scope-local metadata inside `.awm/` only.

## Scope naming

New scopes take a prefix that names the kind of work:

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | One shared frontend component |
| `svc-*`  | service   | One long-running backend service |
| `feat-*` | feature   | Composition that wires components, services and pages together |
| `infra-*`| infrastructure | Toolchain other scopes consume |

Scopes older than this convention keep their flat names.

A scope name may contain `/` when one project holds several products. A nested name has three consequences:

- Pass `branch_name` at create time. The branch takes the scope's name, not `feat/<scope>`.
- Git stores refs as paths. A nested branch forbids a bare branch with its first segment, and the reverse. `scope_create` refuses the collision.
- A project name never nests. A slash in a project name puts a second `.bare` one level down.

References stay `project/scope` and split on the first slash.

A standing `feat-*` scope may own the cross-service wiring for one feature family. It runs its own dev sandbox on a port pinned in a gitignored `awm/gateway/dev/.env`. `dev` is the release-staging worktree, not a feature scope.

## Hubs and peripherals

A hub scope integrates a set of peripheral scopes with two local git operations. **Gather** merges each peripheral into the hub. **Scatter** merges the hub back out. Both are stateless and take the peripheral list explicitly, so this table is the convention they read. Use the `scatter-gather` skill or `scope(verb="gather"|"scatter")`.

| Hub | Branch | Peripherals |
|-----|--------|-------------|
| `feat-dag` | `feat/feat-dag` | `svc-events`, `web-stt`, `web-tts`, `web-ui` |
| `feat-gamebot` | `feat/feat-gamebot` | `svc-effector`, `svc-events`, `rlm-browser`, `rlm-factorio`, `rlm-chess` |
| `dev` | `dev` | all promotable scopes |

A hub may copy its row into its `.awm/context.md`.

## Per-user and shared data

Per-user data is a scope in project `userdata`, at scope `<name>` on branch `user/<name>`. A service that partitions by caller serves every person from one process and resolves the caller per request:

- `awm.config.userroot.resolve(as_)` returns the user, or `None`.
- `userroot.root_for(user)` returns that user's worktree.
- `userroot.state_dir(service, user)` returns the service's index for that user.
- `userroot.wrap_handlers` binds the caller for every verb.
- `awm.config.autocommit` commits the service's subdirectory.

Notes and drawio are the reference implementations.

Shared data never goes in `userdata`. `userroot.users()` treats every subdirectory of `projects/userdata/` as a person, so a shared store there becomes a phantom user. Put shared data in a scope of the project whose code serves it, DVC-pinned under `data/`. One path then moves on a deploy instead of two. The Trilium vault is the worked example: its database and pinned chunks live in `projects/trilium/release` beside the fork that serves them. A node that runs the service without that checkout gets a plain directory at the same path and pins nothing.

## Data

`AGENTS.md` § *Data* has the rules every agent needs. The verbs are on the `scope` and `dvc` domains. Call `describe` for them.

Delete superseded data. An old version stays reachable from the commit that pinned it, so two live copies are never needed.

A project without a tracked `.dvc/config` keeps the legacy `.awm/data` symlink. Nothing migrates it. A project gets wired the first time a scope is created or healed after its checkout tracks a `.dvc/config`.

### Off-site backup

The dvc service schedules two nightly jobs. They do different things.

- **The archive** pushes the DVC cache to chinook. It is append-only, which makes a local `dvc gc` recoverable.
- **The mirror** copies the rest of the workspace to a sibling remote root. It deletes: a file removed here is removed there on the next run. It skips the cache and the materialised checkouts, because the archive holds those bytes and Globus cannot preserve a hardlink.

Every mirror destination sits under `…/workspace/`. A delete-enabled transfer therefore cannot reach the archive, whatever its exclusion logic does.

Neither job replaces pushing a branch. `dvc(verb="coverage")` reports what exists on no remote.

A restore does not return two things:

- Symlinks. The jobs do not follow or recreate them.
- A directory deleted at the workspace root. No transfer item covers it, so it stays on the remote.

## Retiring a project

Archive a retired project. Never delete it.

1. Run `git bundle create <file> --all`.
2. Run `git bundle verify <file>`.
3. Check that every live ref tip is an object in the bundle. A local branch that exists on no remote has no other backup.
4. Store the bundle somewhere DVC-pinned, so the archive job carries it off-site.
5. Migrate the data chunks that matter into a live project.
6. Rename the project to `<name>-ARCHIVED-<YYYYMMDD>`.
7. Add an `ARCHIVED.md` at its top that names where the content went and where the bundle is.
8. Run `chmod -R a-w` on it.
9. Run `scope complete` on each of its scopes, without cleanup, so the worktrees stay on disk as a read-only record.

**CAUTION** The shared cache does not know a project is archived. `data_gc` keeps only what the named projects pin, so a gc that omits the archived project collects its objects. Treat unmigrated chunks as gone.

`project search` lists an archived project twice: under its database name with zero active scopes, and under its new directory name. This is expected. `active_only` drops both.

## Pushing to a peer

Every scope worktree on a peer holds its branch, and `release` is the peer's live workspace. A plain `git push <peer> <branch>` therefore fails with *branch is currently checked out*.

1. Push to a temporary ref on the peer.
2. In the target worktree on the peer, run `git -C <wt> merge --ff-only <tempref>`.
3. Delete the temporary ref.

Use `merge --ff-only`, not `reset --hard`. It refuses when the worktree has uncommitted edits instead of discarding them.

## Submodules

Nothing in the workspace consumes a submodule. If one must, two facts apply that no failure message reveals:

- `git submodule update --remote` follows the `.gitmodules` `branch=` on the default remote. For a local-only sync, run an explicit `fetch` or `push` against the sibling bare.
- `git worktree move` refuses a worktree that contains submodules. Move it by hand:
  1. Move the directory.
  2. Run `git worktree repair`.
  3. Rename the `.bare/worktrees/<name>` admin dir to match.
  4. Fix each submodule's `.git` gitdir pointer and `core.worktree`.

## CLI

The CLI mirrors the whole expanded tool surface. It generates one `awm <domain> <verb>` command per registered tool from the same live catalog as the MCP surface. Run `awm <domain> --help` for a domain's verbs and `awm <domain> <verb> --help` for one verb's parameters.

| Command | Purpose |
|---|---|
| `awm gateway init / status / serve / stop / restart` | Gateway lifecycle |
| `awm project create <name>` | Create a project |
| `awm scope create / list / complete` | Scope worktree management |
| `awm scope heal [--dry-run]` | Idempotent repair |
| `awm scope data-status / data-mount / data-gc` | A scope's data view, what materialises, cache reclaim |
| `awm dvc sync / pull / coverage` | Push the cache off-site, restore one scope, audit what is uncovered |
| `awm gateway register / list / deregister` | External hub registrations |
