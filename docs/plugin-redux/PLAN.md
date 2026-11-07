# Plugin redux: port the core plugins to Rust

## Context

The awm core is Rust, but every plugin is still Python in one shared conda env. Each node refresh pip-installed that env and restarted every plugin. This plan ports the 20 core plugins (the set a typical install ships) to Rust with four workers, each holding a balanced share of about 17k lines of Python. Each worker plans every one of its plugins, this session orchestrates and merges, and then each worker executes. The plugin contract also comes to own each plugin's runtime, so a typical install needs no Python and no package manager. The decisions are journals 1c0f4acc, 2a343d6c, a5f837ac and fc14982b (awm/plugin-redux, tag `decision`).

## What you said

> we moved to rust... why are we even doing pip installs?

> we can allow python, or really anything for plugins, but for the awm core plugins that come with a typical install, if the gateway is in rust but the core plugins are python, then that defeats the purpose.

> since we are porting, we can also take this time to consolidate the code base to make it more coherent

> awm shouldnt be responsible for any package managers like mamba. if a plugin uses it, how can we put that into the contract?

> id like to keep the gateway pure, you can consolidate these, it just has to stay a plugin. gateway is pure logistics.

> dependencies are not used correctly. dependency is a must. / plugins can call other plugins that are not marked as its dependencies

> I expected literally 1 agent per plugin who each plans a rust port for that specific plugin while you orchestrate

> T3 implies 20 workers. please batch according to size to balence work. aim for 4 workers

> opus high for planners / they dont just plan, they execute after planning

> right, since these are all core and live in awm, there can be shared code

## Issues

- I1. The core plugins are Python in one shared `awm` conda env, while the gateway they serve is Rust.
- I2. awm runs a package manager on behalf of its plugins: `awm/install.sh` and each plugin's `install.sh` call mamba and pip.
- I3. A refresh that changes install metadata reinstalls the shared libraries and restarts every plugin. The befc2cf4 stamp skips the install but does not fix the cause.
- I4. A `[[needs.tool]]` entry acts as both a hard dependency and a call allow-list. As a result, core plugins depend at load time on non-core ones (operations on 2fa, trilium and zotero; kb on zotero).
- I5. The board's cards live in the Trilium vault, so the core queue depends on a non-core plugin's store.
- I6. Single concerns are split across plugins: events and routine, channel and relay, and httpsfront, auth and the `tlsfront` library. Three plugins each run a private TLS front.
- I7. The spec's core list (install.md R115 item 4) and its reserved names no longer match the decisions.

## High-level goals

- G1. A typical awm install runs entirely on Rust, with no Python and no package manager.
- G2. Any plugin, in any language, installs through one contract that owns its own runtime.
- G3. The core plugin set is coherent: one plugin per concern, with dependencies that point inward.
- G4. A deploy reinstalls and restarts only what changed.

## Acceptance criteria

- A fresh install in a clean container with no conda, mamba, pip or python3 brings all 20 core plugins to `ready` in `awm services`.
- No core plugin folder holds a `.py` file. A typical install never runs `awm/install.sh`.
- Outside a plugin's own folder, the awm tree invokes no mamba, conda, pip or uv (grep).
- A non-core Python plugin, zotero, installs through its own install hook into its own env and reaches `ready`.
- A refresh with no change restarts no plugin and finishes its deploy step in seconds. A change to one core plugin restarts only that plugin.
- The loader refuses a plugin whose hard dependency is absent. A call to an undeclared but installed tool succeeds. A call to an absent tool returns a `not installed` error. Gateway tests cover all three cases.
- Every ported tool keeps its verbs and argument schemas. A `describe` diff against the Python version shows only intended changes.
- Every ported plugin reads its existing data in place: journal posts, scope records, search indexes and routine history.
- The Rust embedder matches the Python vectors (cosine ≥ 0.999 on a sample), so existing indexes stay valid.
- operations serves the board from its own store, and the migrated card count equals the vault's count.
- The reserved names are `awm`, `auth`, `operations` and `journal`. `auth` is the front plugin.
- install.md R115 item 4, R117, R145 and `plugin-contract.md` state the decided design.

## Tasks

- T1. Rewrite the spec items for the decided design (G2, G3)
- T2. Build the plugin SDK and the gateway contract changes (G1, G2, G4)
- T3. Start the four port workers in plan-only mode (G1, G3)
- T4. Merge the port plans and release the workers to execute (G1, G3)
- T5. Compact
- T6. Land each port through the queue, as merger (G1, G4)
- T7. Compact
- T8. Remove the Python install path from a typical install (G1, G2)
- T9. Verify on a clean install and across the fleet (G1, G4)
- T10. Debrief

## Approach by task

### T1. Rewrite the spec items for the decided design

This task edits `docs/redux/install.md` R115 item 4: the new core list, the consolidations, the not-core list, and trilium as non-core. R115 item 5 drops `2fa` from the reserved names, and `auth` becomes the front plugin. R117 gains the dependency rule: a need is a hard dependency, and calls need no declaration. R117 also gains a code-sharing rule: the core plugins live in awm, so they share library crates in the core cargo workspace. A plugin still never imports another plugin's own crate. Tony: "since these are all core and live in awm, there can be shared code". queue.md R145 changes its storage rule: operations tracks the cards, and trilium exposes and manages them. `drafts/plugin-contract.md` changes its needs guard and install hook sections to match. Each item quotes Tony from the decision journals. This task lands first, so every brief can point at the spec instead of restating it.

Gotchas: board, trilium and the reserved names also appear in channels.md and fleet.md. Grep every mention of "core plugin", "trilium" and "reserved", so no stale sentence remains.

### T2. Build the plugin SDK and the gateway contract changes

This session owns the pieces all four workers share, and builds them in its own scope with subagents. The workers plan against them while they build.

1. **`awm-plugin-sdk`:** extract the register, control WebSocket, ready/call/reply/emit, sessions and mount logic from `core/crates/awm-fake-plugin` into a crate.
   - Its API mirrors `gatewayclient.ServiceAdapter`: a manifest plus a handler table.
   - It also carries the Rust forms of the `config`, `usage` and `telemetry` libraries.
   - The fake plugin becomes its first user.
2. **Contract changes in the gateway:**
   - A missing need refuses the plugin.
   - The needs guard stops refusing undeclared calls.
   - A call to an absent tool returns `not installed`.
   - `2fa` leaves the reserved names (`config.rs:14`).
3. **Runtime contract (`env-contract`):**
   - A core plugin ships as one static musl binary, built on mizar and pinned like `scripts/core.pin`, so a plugin change ships only that plugin's binary.
   - A non-core plugin owns its runtime through its own install hook, with any package manager it chooses. The gateway runs that hook only when the plugin's own install stamp changes, which generalizes befc2cf4 from one awm-wide stamp to one stamp per plugin. awm names no package manager.
4. **Hot-reload follow-ups in the same supervisor code:**
   - `--force` reaches `supervisor::force_start`.
   - An adopted plugin records its env for `stale_env`.
   - Add tests for `stale_env` and the needs-only peer notice.

Gotchas: hot-reload is live in release 5e68a079, so a supervisor change must keep adoption and the restart budget working. The fake plugin's e2e tests are the protocol's regression suite, so keep them green through the extraction. The binary-per-plugin pins must fit the deterministic refresh's stamps, or each refresh restarts every plugin again.

### T3. Start the four port workers in plan-only mode

This task starts four top-level workers with `reflection(verb="start_other")`, on Opus at high effort, joined to task CWPYAtyb. Each gets its own scope. The plugins are grouped by concern, and the groups are balanced by Python lines, counting the shared libraries each group takes over.

| Worker | Scope | Plugins | Shared crates it builds | Python lines |
|---|---|---|---|---|
| comms | `awm/port-comms` | social, channel (absorbs relay), browser, rlm-browser | tether, kept | ~17.3k |
| harness | `awm/port-harness` | cch, reflection, dsh, dev | harness (from `claudedaemon`) | ~17.3k |
| knowledge | `awm/port-knowledge` | journal, scopes, search, kb, transcripts, mic | store (from `persistence`), embed (one ONNX embedder) | ~16.6k |
| ops | `awm/port-ops` | operations, front (absorbs httpsfront, auth and `tlsfront`), routine (absorbs events), dvc, fileviewer, dashboard | trust policy (`trustpolicy` and `grantlib`), dvcdata | ~17.3k |

Each brief carries Tony's words, the decision journals, the T1 spec items, the SDK interface and the worker's plugin list. It sets the rules:
- Keep the tool name, its verbs and its schemas.
- Read existing data in place.
- Declare only hard dependencies.
- Consolidate within the group where that makes it more coherent.
- Leave no Python behind.

The first deliverable is one written port plan per plugin, sent to this session before any code. Each brief also adds what is specific to its group:
- ops: operations' own queue store and the card migration from the vault (fc14982b), dsh's move behind front, and dvc's schedule moving onto routine.
- harness: `claudedaemon`'s reversed import of cch.
- knowledge: the embedder parity check.

Gotchas: workers plan while T2 builds, but none executes before the SDK lands. Concurrent cargo builds share one build container and cache on mizar, so four workers keep that load bounded. A question only Tony can answer goes to `request_human`, one per issue.

### T4. Merge the port plans and release the workers to execute

Revised 2026-11-07 (Tony: "each worker proposes plan I ok each individually" and "you can check plans too before they submit to me"). Each worker sends this session its plan path and its cross-group questions. This session checks that plan on its own, against the spec rules and the other groups' call shapes, and answers. When the plan holds, this session tells the worker "submit", and the worker submits it with ExitPlanMode. Tony approves each plan individually. This session answers each cross-group question in the worker's thread and settles conflicts between plans as they surface, such as harness ↔ reflection ↔ social and knowledge ↔ every caller of journal and scopes.

Gotchas: knowledge's store and embed crates and ops' trust-policy crate are needed by other groups. Those crates land first, ahead of any plugin that uses them. No port executes against the SDK before the SDK lands.

### T5. Compact

Handoff:
- `docs/plugin-redux/STACK.md` lists the four workers' job ids, scopes and states, every shared crate with its owner, and each plugin's plan state.
- The todo list is at T6.
- Facts a re-read will not recover: the cross-group conflicts found in T4, and how each was settled.

### T6. Land each port through the queue, as merger

When the queue names this session merger, run `land.sh batch` under ci/merge. Fix integration faults in the batch worktree. Bounce a branch's own fault to its worker. Each port deletes its Python folder in the same commit, so no plugin runs a Python copy and a Rust copy side by side. Each worker runs the live check for each plugin it lands, and retires after its last plugin.

Gotchas:
- A port that changes a data format migrates on first start and keeps a backup.
- A plugin's first Rust deploy on each node counts as an install, so expect one longer refresh per node.
- front replaces the TLS edge, and social and cch hold live seats and sessions. Land each of these in a quiet window. Check the login page, `social keepers` and a live harness session right after.
- Relay's first deploy empties the rooms once.

### T7. Compact

Handoff:
- STACK.md lists which ports landed at which release, and which are still out.
- The todo list is at T8.
- Facts a re-read will not recover: any node that needs a hand fix.

### T8. Remove the Python install path from a typical install

Once no core plugin uses Python:
- Delete the core half of `awm/install.sh` and the shared conda env setup.
- Delete each Python `service_components` library that only core plugins used.
- Give each non-core Python plugin (2fa, trilium, ssh, vpn, wake, engcyc, rlm-factorio, zotero, claude-science) its own install hook that builds its own env, with the Python SDK pinned in that env (R117 item 6).

Moving the non-core plugins into projects of their own stays with R33's `plugin_homes`. So does the trilium vault view of the cards.

Gotchas: alcor, deneb and other nodes still run some non-core plugins, so each plugin's own env must exist before the shared env goes. Check `awm wants` on each node first. Never delete a store under a plugin's state dir.

### T9. Verify on a clean install and across the fleet

Run a fresh install in a clean container with no conda or pip, and check every acceptance criterion. Refresh every node twice: the second refresh logs `install: no` and restarts nothing. Make a one-line change to a core plugin and confirm that only that plugin restarts.

Gotchas: lyra's refresh fails because its checkout has no `awm` remote. Fix that node before fleet verification.

### T10. Debrief

Run the `debrief` skill. It closes STACK.md, journals the redux and cites the decision journals.

## Callouts

- The handed-over follow-ups stay on the stack, and I clear them between merges: lyra's missing remote, mizar-push, stock's `deploy.toml` `kind = "none"`, the deneb relay linger and the hot-reload untested paths. The supervisor items moved into T2.
- The search plugin logs about 3,900 "needs guard: refused <x>.describe" lines. T2's needs-guard change removes the cause.
