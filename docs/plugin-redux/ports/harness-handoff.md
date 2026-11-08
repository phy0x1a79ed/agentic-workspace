# Harness port: handoff

## Purpose & Contents

This file hands the harness group port (plan: `~/.claude/plans/sparkling-sniffing-sonnet.md`, mirrored in this folder's `awm-harness.md`, `dev.md`, `reflection.md`, `dsh.md` and `cch.md`) to whoever takes it next. It states what landed, what is built but not landed, and the open items. Remove it when the last harness plugin has landed and every open item is closed.

## Landed

Land batch cffff968f16f promoted release `4988f68ab2ef` with feat/port-harness (ticket 675643a19fdb), feat/ssh-vpn-jump and feat/port-knowledge-mic. It bounced feat/port-ops (ticket a02661bd9972), because that branch's `scripts/run-tests.sh` still lists the deleted `dvc` dist.

- cch, dsh, dev and the awm-harness crate run as Rust binaries from `core/plugins/`. The Python cch, dsh, dev and `claudedaemon` are deleted.
- cch writes its four hooks into `~/.claude/settings.json` as `cch hook <name>` at start. `awm/scripts/deploy-hooks.sh` carries only the other plugins' hooks.
- cch and dsh take the `.pre-rust/` snapshot themselves through `cch state` and `dsh state`.
- The sessions' `PATH` takes `[env] AWM_SESSION_PATH`, else the running daemon unit's extra dirs. No conda knob is left.

## Live check

The mizar live check passed all nine steps of the land brief. mizar and alcor run `4988f68ab2ef`, and alcor's cch reconciled its hooks. The batch was still rolling out to capella, lyra, deneb, shogun and altair at handoff. Read `.awm/land/awm/logs/cffff968f16f/` on mizar for each node's result.

**WARNING** The first mizar rollout failed. cch refused to start, because awm-statecheck cannot copy a directory named as an `[[extra]]`. While cch was down, every awm call on mizar got 403 "mode unknown" for about 4 minutes. The batch carries the fix: cch names reflection's `pending/*` as a glob.

## Built, not landed

- **dsh session events** (`1424ab24` on feat/port-harness). dsh sends `started`, `stopped` and `renamed` to reflection `harness_event`, behind the `session_kind` gate. It lands with the next feat/port-harness ticket.
- **The Rust reflection** (branch `wip/port-harness-reflection`). The crate compiles. Its ported dispatch tests are unfinished. The R1 brief, the R2 brief and their rules sit in the crate folder. The crate is kept off feat/port-harness, because the workspace glob would build it.

## Open items

1. Finish the reflection port from `wip/port-harness-reflection`: the R1 tests, then brief R2 (the sessions topic, sender names with `to`, and the orders pull). Then delete `awm/services/reflection`, add `scripts/plugins/reflection.pin` with its `deploy.toml` line, and drop the reflection dist from `scripts/run-tests.sh`.
2. Settle the order puller with ops before reflection lands. The Rust operations on feat/port-ops still runs its own `order_loop` for `agent` orders. With reflection's puller beside it, each order starts two sessions (`reflection.md` § Orders, **One owner**).
3. Fix awm-statecheck so a directory `[[extra]]` copies (ops owns the crate). Until then, every `state.toml` names a directory's files with a glob (`harness-state.md` rule 3).
4. Check the dsh fork's `events.mux` frame names against a live fork. dsh's lifecycle diff triggers on names read from a public client, not from a call.
5. Give the miniforge nodes `AWM_SESSION_PATH` through homelab_base (plugin-redux/interview's T8).
6. Remove the `.pre-rust/` and `.rust-<ts>/` copies a week after the last harness land passes its checks, only with Tony's OK at that time.

## Skipped

Tony stopped new lands, rollouts and Ansible runs for this wrap-up. The steps below were therefore not run:

- the land of the dsh session events and of the Rust reflection;
- the reflection live check on mizar and capella;
- the homelab_base `AWM_SESSION_PATH` change.

Ticket 3e3bc12a2040 stays queued for the next merger.
