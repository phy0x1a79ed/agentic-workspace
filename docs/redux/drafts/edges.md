# Draft: keys and routes per node

## Purpose & Contents

This draft turns the edge table (`fleet.md` R87) and the three key levels (R92) into the `authorized_keys` lines and `~/.ssh/config` routes of each node. It holds the shape only. Install nothing from it before the implementation phase. The network column follows `fleet.md` R95 and R96.

## Key lines

Each `authorized_keys` line carries a comment of the form `awm:<holder>:<level>`, so a log line names the holder and the level.

- **User** (an edge): `ssh-ed25519 AAAA… awm:mizar:user`
- **Service** (singletons and knowledge): `restrict,command="awm port",expiry-time="<date>" ssh-ed25519 AAAA… awm:altair:service`
  - A node's own service key for a singleton host carries no `expiry-time`. A timed invite carries one.
  - `awm port` reads the requested verb from `SSH_ORIGINAL_COMMAND`. It refuses any verb outside the singleton and knowledge sets, and it logs each call.
- **Admin**: a separate account with sudo. `awm:shogun:admin` on the Arbutus nodes, `awm:mizar:admin` on the fleet.
- OpenSSH supports `expiry-time` from 8.2. mizar, alcor and altair run 9.6, and mira runs 9.7.

## Who holds a key on each node

| Node | User keys from | Service keys from | Admin key from |
|---|---|---|---|
| lyra | mizar, capella | timed invites | — |
| capella | mizar, lyra | timed invites | — |
| mizar | alcor, lyra, capella | timed invites | — |
| alcor | mizar, lyra, capella | every node (singletons) | mizar |
| altair | mizar, lyra, capella | timed invites | shogun |
| deneb | mizar, lyra, capella | every node (board, relays) | mizar |
| sockeye, fir | mizar, lyra, capella, altair | — | — |
| shaula, workers, showtime, bastion | open (shogun's map) | — | shogun |

sockeye and fir take keys through the Alliance account, behind Duo. Their lines are not ours to edit.

## Routes from each node

All routes keep `ControlMaster auto` with `ControlPersist`, except an admin login, which never shares a connection.

| From | To | Network |
|---|---|---|
| any home box | mizar, alcor, mira, capella | LAN, at an address the router reserves |
| any home box | capella | LAN, port 22 forwarded from capella's LAN address to `127.0.0.1:22` in WSL |
| any node outside Arbutus | altair, Arbutus nodes | internet, `ProxyJump bastion-01` |
| altair | Arbutus nodes | the Arbutus private network, direct |
| any node | deneb | internet, deneb's public address |
| any node | sockeye, fir | internet, as today |
| lyra | any home box | Tailscale |
| home boxes | lyra | Tailscale |
| altair | macpro, and back | ZeroTier |
