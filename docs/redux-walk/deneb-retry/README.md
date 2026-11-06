# deneb-retry

## Purpose & Contents

The second attempt at deneb's cutover after the first rolled back on 2026-11-05: why it failed and the order the retry runs in.

## Findings (2026-11-05)

- deneb on f5c5294d refuses its plugins: they need operations and social, which live on alcor, and only door-client meets a need through a singleton's door.
- alcor on c175abb2 refuses operations for the mirror reason: operations needs trilium on deneb. alcor's door and keys are fine. It hands calls to a gateway that has no operations loaded.
- A need on a door singleton holds while that door is down (dev), which breaks the cycle.
- Retry order: alcor on the door-client release, then deneb's cutover on the same release, then mizar in a later window.
- mizar's edges are off by a drop-in (`edge-off.conf`, AWM_EDGE_SSH=/bin/false), which is why mizar's operations and trilium calls fail with "Broken pipe". Whether it stays is open with dev.
- The retry needs Tony's go for a new window (Q-deneb-retry).
- Rollback verified (deneb-board): deneb back on 3efb22b1, vault 240|1765|9, Trilium, zotero and tether up. Step 2b (sshd, ufw, SNI) stays.
- deneb-board's view, adopted: the retry is one window. mizar on 60a1e5f8 would lose trilium and zotero once they sit behind deneb's door. The order is mizar, then alcor (operations serving), then deneb's cutover.
- The release candidate must have: the door login user from `singleton_doors.<host>.user`, needs holding at install while a door is down, a met search need for deneb's journal, and the awm-deploy.sh chmod fix.
- The cutover's rollback repoints trilium's node-bin by script. The avatar relay moved to a unix socket in 2b, so capella's client must switch (Tony's M44).
- R79 governs deneb's plugin set: "deneb runs no agent session and opens no outbound ssh." deneb therefore makes no door call. Its needs on operations, social and search become contract needs that may have no provider (dev), and its plugin list drops the agent machinery (deneb-board). deneb leaves every singleton's callers list.
- Door login user: door-client reads `singleton_doors.<host>.user` (e4e66e6b). A need on a door singleton holds at load, and the door is never contacted at load (Rust test `a_singleton_behind_a_door_strands_no_plugin`).
- Each caller pins each door's host key, recorded from the host itself, never by keyscan (deneb-board's role).
- mizar's edge-off.conf comes off in the retry window, after alcor runs the release.
- dev's code side (073c8874, in the RC as accc9460): search, operations and reflection implement contracts of their own name. journal, channel, board and zotero need contracts. trilium's stale operations need is gone. The backup-diff hold already runs on mizar (`awm.trilium.backup pull`). On deneb, board intake and zotero's order puller idle. The Rust test `denebs_plugins_load_with_no_door_and_no_peer` passes.
- Two features need their direction inverted to satisfy R79. They are separate topics: zotero-order-invert and board-intake-invert. Neither blocks the retry: alcor's approver is not live yet, and board events queue without loss.
- Tony set the aim to the end state, with no interim work: "Yeah, let's forget about making things work in the interim and just focus on getting the end state, like getting towards the end state." (2026-11-05)
- The window must leave the rooms service (channel's persistent rooms) up, so the agent on alcor can teach the rt agent the new system. Tony: "One thing for the Alcor cutover is we need the rooms service to be up so that the Alcor agent can teach the RT agent, which was the original session that created the RT stock stuff, how to actually use the new system and edit. So we need the room service up." (2026-11-05)
- The window's checks therefore include a persistent room that a session on alcor and a session on mizar both join, which survives a restart of channel on deneb.
