# board

## Purpose & Contents

This topic holds the path to a live board: the Trilium command center page (queue.md R61) and the services it needs on alcor and deneb.

## Tony's words

> the "One file holds everything" is not a hard blocker, can we look into setting up trillium and the new board then? this would include the alcor services

## Survey (2026-11-05)

- Built and on release dcd8ba4d: the operations plugin (approver, panel, `board-install`, writer, relay, door) and the trilium service.
- Built in cloud 58339c4, applied nowhere: alcor's `approver_config`, `auth_tunnel`, `singleton_door`, `singletons_sandbox`, `alcor_ssh_door`. deneb's SNI forward, `deneb_passkey_login`, `trilium_deneb`, `trilium_backup`, `trilium_copy_stop`.
- Missing: the Trilium fork on mizar (`projects/trilium`) and its release, so `trilium/server.pin` is empty. `approver_grant_pub` is empty. `approver_portals` names only nexus.
- Live: mizar reaches neither alcor nor deneb. The edge-off drop-in (`AWM_EDGE_SSH=/bin/false`) stops every edge call, and both nodes run the old release. mizar's `config.toml` leaves out the operations relay, and the old `board` service still runs.
- Tony's steps on the path: M10, M12, M13, M14, M19, M25, M29, M33, then M42, M49, M50.
- Held out: alcor's rt play, `mira_copy`, M30, M31. None is a prerequisite.

## Decisions

- alcor opens for every service except the rt services. Discord may stay blocked on alcor too. Tony: "alcor for all except the rt services, we may also have to block discord as well" (2026-11-05). mira stays untouched.
- Discord stays off alcor until mira's cutover. Tony: "discord OK" (2026-11-05).
- redux-build/dns-guide (sonnet, medium) walks Tony through M10 and M12 to M14. Tony: "start a sonnet agent to walk me through M10 and M12-14" (2026-11-05).
- alcor dry run (alcor-open, cloud af1aceb): go for the backup, the release, journal_move, the deploy and every role but two. users_sudo is held as Tony's M29, because it changes the ssh admin gate. alcor_door waits on Tony. social runs with no accounts.
- deneb: backup 1 is on mizar (pre-20261105T1949Z, integrity ok, 1765 notes). A push node does not build: mizar ships the built pages and the core binary as pinned artifacts, and the node runs `awm deploy --no-build` (core-artifact). deneb gets socat and no Node.
