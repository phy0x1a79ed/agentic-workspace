# Redux finish — issues

Problems found while finishing the redux, for Tony to read at any time. Each entry: what is wrong, where, and what happens next. Newest last. An entry that needs Tony says **needs you**.

## Open

1. **Stalled land batch.** Batch cffff968f16f promoted release 4988f68a and rolled out to mizar and alcor, then its merger (port-harness) was shut down during capella's refresh. The lease still names ticket 675643a19fdb, so nothing else can land. capella's checkout is at 4988f68a, but its deploy step may not have run. Next: `batch-close`.
2. **Python still runs most core plugins.** On mizar: auth, httpsfront, dvc, journal, kb, reflection, scopes, search, transcripts, channel run Python. On alcor: the operations, social, browser and journal singletons run Python. Rust today: cch, dsh, dev, fileviewer, mic, routine, telemetry.
3. **kb and scopes were never tested.** They pass `cargo check` only. clippy fails on `absurd_extreme_comparisons` in awm-index. journals has no test target. Next: `land-kb`.
4. **Two half-finished ports.** reflection (wip/port-harness-reflection: compiles, tests unfinished) and auth/front (feat/port-ops-t9: does not build). Next: `finish-reflection`, `finish-auth`.
5. **Two order pullers.** The Rust operations runs its own `agent` order loop; the Rust reflection plans a puller too. Both live means two sessions per order. Settle before reflection lands.
6. **awm-statecheck cannot copy a directory `[[extra]]`.** It took mizar's cch down for 4 minutes on the first rollout (every awm call got 403). Worked around with globs. Owner: the statecheck crate.
7. **SDK lint.** The plugin SDK fails `clippy::result_large_err` (17 hits in hub.rs and service.rs). Crates lint with `-A` until fixed.
8. **routine from a scope worktree.** `routine state check` and `routine land` with `AWM_DIR` unset take the scope as the workspace. land.sh guards it; the bare binary does not.
9. **cloud repo drift.** GitHub's cloud `dev` (8f79bec) is ahead of mizar's cloud bare (6c01e03). Merge it before any cloud branch.
10. **run-containers.sh cannot run on mizar.** The `community.docker` Ansible collection is missing.

## Closed

(none)
