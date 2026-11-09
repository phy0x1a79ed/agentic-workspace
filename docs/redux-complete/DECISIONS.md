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

## node-checkout (2026-11-08, rewritten after node-checkout-defer)

Question: under the post-Rust redux, do nodes still need the awm git clone? (Tony's question, raised during the walk.)

Tony first asked for "an install system so that running awm with core plugins doesnt need a git clone". The walk settled a design: a release of hash-addressed binaries plus a small tar, unpacked with no git, carried by mizar push and a GitHub Release, with the non-core plugins in projects of their own pushed as folders.

Tony then deferred it: "Non core plugins made me realize that we are not ready to drop the git repo correct? ... We can defer the change and keep using git if thats easier to manage". It is easier, because the design put a release format, a gateway and deploy rewrite and eleven new projects ahead of the fleet refresh.

Decided: nodes keep the awm clone in this pass, moved only by the refresh. The non-core plugins and the sites daemon stay in the clone at their current paths. The clone-free install is deferred past the redux as a design to pick up later. Nothing of it gets built here, and done-line's "Everything" excludes it.

Landed: this file. Nothing to build.

## open-for-tony (2026-11-08)

The end-state journals' open items not settled above.

- **cch warm pool.** Tony chose "Dedicated seed dir, cch trusts". cch records trust for one seed directory with `trust::grant_plain_dir`, as it does for the lobby, and `AWM_CX_SEED_DIR` defaults to it. Tony accepts that this extends trust to `--dangerously-skip-permissions` sessions.
- **`isolated/` mirror tag.** Overtaken: Tony said "Stop the dvc nightly", then chose to stop the mirror only. `workspace_backup` stops on altair and stays off, so `.awm` and credentials stop going off-site. `cache_sync` keeps running. Workspace files outside DVC have no off-site copy after this. This overrides end state 8's mirror sentence.
- **claude-science idle.** Tony: "Science timeout to 2hrs". It starts on the first request and stops after two hours with no request.
- **lyra's `vpn.toml`.** Copy capella's, as mizar's was copied: 0600, hashes checked, nothing printed.
- **alcor's sites daemon.** "Mira is going away" does not cover it. It stays at its path in the clone (node-checkout, as rewritten). Its error loop goes to the quality backlog.
- **per-node-edges.** Every edge on every node. Each node's installed `edges.yml` holds only the peers it can reach, rendered from `homelab/edges.yml`. Fleet-wide lists such as the land rollout come from mizar's install config. Singleton callers and relays read what their own node has.

Landed: to be done by the plan.

## close-out (2026-11-08)

Questions: when do the `.pre-rust` snapshots go, what happens to the old scopes, and do seats come back on?

Decided:

- **`.pre-rust` and `.rust-<ts>`.** Tony's OK, dated 2026-11-08, is conditional: each node's copies are deleted only once that node has passed its live checks at the end of this pass.
- **Old scopes.** Scopes whose branches are fully in release are completed and their worktrees removed. Branches stay in git. Each unmerged branch is audited for anything the end state still needs, and the audit goes to Tony before anything unmerged is touched.
- **Seats.** Unpaused as the last step, after the fleet is verified, with one live inbound and outbound message confirmed.

Landed: snapshots deleted on mizar, shogun, capella, altair and deneb. alcor keeps its copy until Monday's close, and lyra is offline. 90 merged scopes retired, and the branch audit went to Tony on card 46Pz69LI. The seats stay paused: social on alcor has no edge to altair, where R129 puts the Slack seats, and altair's `managed: false` reads as "runs no awm" (cloud 3e6f469). Tony chooses an alcor to altair edge or moves the Slack seats to mizar.
