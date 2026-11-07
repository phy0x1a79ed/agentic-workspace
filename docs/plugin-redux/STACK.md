# Plugin redux — interview stack

Goal: settle what the awm plugin redux covers and how plugins install and run now that the core is Rust, then submit a formal plan. Prompt (2026-11-06): "we moved to rust... why are we even doing pip installs?"

Decisions: `search` for journals tagged `decision` in awm/plugin-redux.

## Current

- `build` — executing docs/plugin-redux/PLAN.md (approved 2026-11-06). Next: T1 (spec items), then T2 (SDK + contract) with T3 (four workers plan-only) in parallel.

## Waiting on the user

- (none)

## Preferences

- 2026-11-06: four workers, balanced by size, not one per plugin. Core plugins may share crates.
- 2026-11-06: planners are Opus high, and they execute after planning; this session merges.
- 2026-11-06: run the walk with dfs-interview. "when we have things scoped out, formally submit a plan."

## Stack

- `build` — the approved plan, T1–T10 (todo list mirrors it). `env-contract` is folded into T2 item 3.
- `hot-reload-followups` — from hot-reload/interview (journal 417d8c1c, docs/hot-reload/STACK.md § Follow-ups on feat/hot-reload): --force on reload/restart never reaches supervisor::force_start; adopted plugins have no env for stale_env; untested paths; deneb relay linger; batch 350ebc9b49c4 settled by the redux-fixes merger (all nodes at 5e68a079).
- `refresh-handoff` — deterministic-refresh LANDED in release 5bbbd3ae (batch 41e6940e4f5b, 2026-11-07 00:40Z); live: six nodes log `install: no`, deploy 1–22 s. Open: lyra refresh fails (`fatal: 'awm' does not appear to be a git repository` in its checkout); mizar-push (decided, unbuilt; would fix lyra's class of fault); stock deploy.toml `[refresh] kind="none"` fix.

## Workers

- (none)

## Closed

- `core-set` — journal 2a343d6c (core set, consolidations, wake not core, kb own source plugin, reserved names).
- `deps-semantics` — journal a5f837ac: a need is a must; calls need no declaration; no optional flag.
- `board-store` — journal fc14982b: operations tracks the queue state; trilium exposes and manages it.
- `plan` — journal 5d8bd9db: four Opus-high workers by concern (~17k lines each), shared crates allowed in core; PLAN.md.
- `rust-port` — journal 1c0f4acc: core plugins port to Rust; any language for others; consolidate; per-plugin planners.
