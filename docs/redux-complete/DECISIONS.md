# Redux complete — decisions

Closed topics, in the order they closed. `STACK.md` beside this file holds the open state.

## done-line (2026-11-08)

Question: what counts as the redux being finally complete?

Tony chose "Everything" from four options.

Decided: the pass covers all four groups, and nothing stays open after it.

- (a) end-state code: port-harness, SDK snapshot, Python out of core, routine events.
- (b) every node refreshed to one release, including the deneb vault board.
- (c) the old walk's per-node hand items: Windows bridges, shogun admin edges, Ryan's keys and the rest of the parked per-node work.
- (d) the quality backlog: telemetry seat catch-up, reconnect backoff, the kb first-start gate and the rest of the batch 2 simplify list.

Landed: this file. Scope of the plan in `~/.claude/plans/review-the-chain-of-dreamy-sutherland.md`.

## land-port-harness (2026-11-08)

Question: does feat/port-harness land? Decision 8 of redux-finish held it "until Tony chooses to land it".

Decided: it lands. done-line's "Everything" names it. Landing follows journal ace5dab4's steps, with c058b6e5 and 60fce751 (a start needs a human's permission) on board. Reflection's order puller and the removal of operations' `order_loop` ship in one release.

Landed: to be done by the plan.

## fleet-route (2026-11-08)

Question: how do the other nodes come up, and where does the cloud repo's `dev` live?

Decided: land on mizar and alcor and check them live, then refresh altair, shogun, capella and lyra together and fix by patch. deneb is its own cutover, because it needs a layout change and carries the public relay. The refresh's 90 s up-check is fixed before this, since kb's first start takes 5 to 11 minutes.

The cloud repo's canonical `dev` moves to mizar's bare repo. GitHub stays the backup and altair follows, the same rule as awm.

Landed: to be done by the plan.

## r79-writer (2026-11-08)

Question: how does deneb's vault writer get alcor's cards and roster, given R79 ("deneb runs no agent session and opens no outbound ssh")?

Tony: "Push makes the most sense here". This replaces a mistaken "Amend R79" answer given earlier in the same discussion.

Decided: alcor pushes. operations and telemetry on alcor call hidden trilium verbs on deneb (`board_apply`, `ops_render`) on each event, through the door alcor already uses. deneb holds no service key and makes no outbound call. The writer's note code on feat/trilium-vault stays, and only its input changes from a pull to a push. R79 and its cloud test stand unchanged.

Landed: to be done by the plan.
