# ops-board: the vault's boards after the redux

> https://nexus.tony-xy-liu.com/trilium/#root/iF0A3SWEGqUx/P4ALwPiCSs6N?ntxId=CZkmWI ops board needs updating

> lets also clean up the federation board and prepare for use

## What the spec says

- `fleet.md` R81: the Ops page holds a static link to each box's portal and no password.
- `fleet.md` R105, R106 and `queue.md` R61, R62: alcor's operations writer mirrors the task queue into deneb's vault as the Swarm board. The board's root is the note `awmSwarmBoard`. Its Ops book is `awmSwarmOps`, which holds the services' notices and a Portals note. The writer may write only under `awmSwarmBoard` (`AWM_TRILIUM_WRITER_ROOTS`). The operator's `board-install` creates the board page.
- `channels.md` R77 and `awm/services/board/INSTALL.md`: the federation board (`#federationBoard`) stays until Caviar and muse move onto persistent rooms. Its door on deneb takes foreign swarms' cards. Intake turns each one into a Proposed card on the Swarm board.

## State on 2026-11-06

- The vault holds the pre-redux AWM book (`iF0A3SWEGqUx`): Major updates, Federation board (`0TIhqmkIu3Oc`), the old Swarm board (`Nzv8wkoUgl2V`) and the old Ops (`P4ALwPiCSs6N`).
- The old Ops holds seven notes: `password - <node>` for capella, altair, mira, VM-0-8-ubuntu (deneb), mizar and alcor, last written 2026-10-27 to 2026-11-05 by the pre-redux services, plus a Links note (2026-10-15). R81 rules out passwords on the Ops page.
- `awmSwarmBoard`, `awmSwarmOps` and Portals do not exist. alcor's writer fails every step: its first `note_get` on the missing root raises, because deneb's door returns ETAPI's 404 as a 502, which the gateway client raises as Unreachable, not Refused. No card reaches the vault. Fixed in awm 5aff3f26.
- The writer cannot create `awmSwarmBoard` itself, because a note under `root` is outside its subtree. The operator creates it.
- The federation board has 7 columns. Live cards: Proposed holds 7 (W1, W2, CL1, factorio-diffusion read access, factorio-diffusion stage 2, deneb access, TreeSAPP-AMBER runtime). Planned holds 1 (Opal on Slack). Each column also has an Archived or Older note.

## Tony's answers (2026-11-06)

> 2 I never used the old swarm board, so it can just be deleted. I havent looked at the new one yet. why are we migrating rather than updating the ops section?

> 3 can just delete, never used. lets hold a full test of the new queue and approvals workflow in 1 sitting, prolly with another agent

> 4 what is the awm book? what is the new swarm board?

- Delete the old Swarm board (`Nzv8wkoUgl2V`).
- Delete the federation board's 8 pre-redux cards. The full test of the queue and approvals runs as its own topic, `end-state/queue-approvals-test`.
- Still open: whether the new Ops replaces the old one, and where the new board goes. These wait on the answers to "why migrate" and "what is the AWM book".

## Tony's answers, round 2 (2026-11-06)

> the new swarm board is bugged and not showing anything. there's also a bunch of just folders under swarm that look like tasks. they are not organized

> ops can be the new swarm status page I requested and all of our nodes including stations register to it.

- Cause: after 5aff3f26, alcor's writer created `awmSwarmBoard` under `root` at 17:58Z and wrote the cards. `board-install` had never run, so the root had no page to render and each card showed as a bare note. Ran `python -m awm.operations.cli board-install` on alcor at about 18:20Z. The root now has `renderNote` → `awmSwarmBoardPage`.
- Ops (`awmSwarmOps`) becomes the swarm status page: a roster of every node, stations included, each registered with its stats. That absorbs `end-state/node-roster`.
