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
