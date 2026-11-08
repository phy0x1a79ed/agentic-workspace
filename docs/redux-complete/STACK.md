# Redux complete — stack

Goal: settle how to finally complete the redux and deploy it fleet wide, with the end-state journals as the target. Prompt (2026-11-08): "Use dfs interview with me to settle how to finally complete the redux and deploy it fleet wide. Please then do the work yourself, but you may use sonnet medium agents for specific tasks."

Target: journal d2bb0e4d (index) and its eight replies. The plan file is `~/.claude/plans/review-the-chain-of-dreamy-sutherland.md`.

Decisions: `DECISIONS.md` beside this file.

## Current

None. The stack is empty, and the plan is next.

## Waiting on the user

None.

## Preferences

Carried from `docs/redux-walk/PREFERENCES.md` (2026-11-05): build for the end state, no interim work; deploy on many fronts and fix by patch; keep the dry-run and held login only for changes that can lock out a node (sshd, firewall); unit tests for touched code, then test live; work in passes.

Tony (2026-11-08): this session does the work itself; Sonnet agents at medium effort for specific tasks.

## Stack

Empty.

## Workers

None.

## Closed

- done-line — everything: end-state code, every node, per-node hand items, quality backlog
- land-port-harness — lands, per ace5dab4
- fleet-route — mizar and alcor first, then altair, shogun, capella, lyra together; deneb apart; cloud dev moves to mizar
- r79-writer — alcor pushes to deneb's vault writer; R79 stands
- deneb — no further decision; its own cutover per fleet-route, writer per r79-writer
- node-checkout — every node clone-free this pass; release = manifest + hash-addressed binaries + small tar; mizar pushes, GitHub Release for fresh machines; ten non-core plugins in own projects, pushed as folders
- open-for-tony — warm pool on a trusted seed dir; dvc mirror stopped; science idle 2 h; lyra vpn from capella; sites daemon kept as own project; per-node edges everywhere
- close-out — `.pre-rust` goes after each node's checks; retire merged scopes, audit unmerged; seats on last

## Survey state (2026-11-08)

- mizar and alcor run release 7fd92893, all services ready at the last refresh.
- shogun and altair run acadb279, 180 commits behind, Rust gateway, ok at their 2026-11-07 refresh.
- deneb has no `awm.service`. It runs `awm-plugin@relay`, `@trilium`, `@zotero` from `/opt/awm`. Disk 83%, 13 G free.
- capella woke on request (2026-11-08): 139 behind, 292 G free. Tony wakes lyra.
- feat/port-harness 60fce751 is 23 commits ahead of release, 4 behind. Landing steps: journal ace5dab4, plus c058b6e5 and 60fce751 (start needs a human's permission).
- Code gaps that do not need Tony, to go in the plan: a83311d1 SDK snapshot is in no tree; `reflection.pin` is missing; `awm deploy` still calls `awm/install.sh`; `awm/services/board` tracked; routine has no event trigger; cloud f6d8279 unmerged and `redux-dev-apply` holds cloud dev; cloud `ExposeAuthInfo`, ZeroTier, conda dvc, `AWM_SESSION_PATH`; 90 s up-check against kb's first start; 02b25012 unmerged; scatter-gather verb names.
- The journal's "mizar push not built", "opt-in open", "operations session hook" and the audit's gap 3 are out of date against code and DECISIONS batch-3 item 3.
