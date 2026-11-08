# Redux finish — stack

Goal: bring mizar and alcor to the post-redux state in this one session, from the work the plugin-redux workers left. Other nodes wait. Prompt (2026-11-07): "can we finish the work here, in 1 session? … Lets get mizar (and alcor since it holds crucial services) to the post redux state before worring about other nodes."

Decisions: `DECISIONS.md` beside this file. Issues for Tony to read at any time: `ISSUES.md` beside this file.

Sources: the worker debrief journals 389a0111 (ops), 8caec34d (knowledge), 449af06d (harness), 004f4c13 (comms), f9c57c47 (trilium), 4bbcc79a (hpc-ssh-guard), 3781f166 (coordinator), and the handoffs they name.

## Current

- `done-and-rollout` — what "post-redux" means for mizar and alcor, and how lands roll out while the other nodes wait.

## Waiting on the user

- `done-and-rollout` questions (asked 2026-11-07).

## Preferences

- 2026-11-07: one session does the work. Sonnet-medium subagents take well-defined tasks.
- 2026-11-07: track the walk with dfs-interview. New issues go to `ISSUES.md`, not a question each turn.
- 2026-11-07: do not use the board; it is being remade. reflection and journals work.
- 2026-11-07: mizar and alcor first. Other nodes later.

## Stack

- `done-and-rollout` — define done for mizar and alcor; narrow the rollout.
- `batch-close` — the stalled batch cffff968f16f (lease on dead harness ticket 675643a19fdb; rollout stopped at capella).
- `land-dvc` — ticket 68647284ec63 (feat/port-ops-t7), then T7 live checks.
- `land-plain-ssh` — ticket 3e3bc12a2040 (feat/plugin-redux: plain-ssh edge, SDK snapshot, workspace root). Old and new nodes refuse each other's `--from` until both refresh.
- `land-operations` — feat/port-ops + cloud feat/ops-units on alcor; order-loop owner vs reflection.
- `land-comms` — social, browser, channel, rlm-browser + cloud feat/rust-singletons (alcor, deneb relay).
- `land-kb` — kb + scopes: untested, journals has no tests, clippy fails; cloud feat/kb-journal-unit; model files; tag clash.
- `finish-reflection` — wip/port-harness-reflection: R1 tests, R2; dsh session events ride with it.
- `finish-auth` — feat/port-ops-t9: front plugin (auth + httpsfront + tlsfront) about half ported.
- `python-out` — plan T8: drop the core Python install path; non-core Python plugins get their own envs.
- `verify` — plan T9 checks on mizar and alcor.
- `shelved-decisions` — R79 vs vault, per-node-edges, lyra vpn.toml, isolated/ tag, alcor's sites daemon.
- `later-nodes` — capella, lyra, deneb, shogun, altair; trilium vault; telemetry cloud batch; hpc-ssh-guard apply.

## Workers

(none)

## Closed

(none)
