# Fleet

## Purpose & Contents

This doc holds the nodes of the fleet, the role of each, and how each node is hardened. Thread 1r produces the hardening of deneb, the public host. Thread 1g produces how the nodes communicate.

## Requirements

- **R79.** deneb runs no agent session and opens no outbound ssh. Tony: "Agents and outbound ssh should stop though" (2026-11-01).
- **R80.** deneb keeps the vault. Tony: "Deneb needs trillium." (2026-11-01).
- **R81.** The Ops page holds a static link to each box's portal and no password. Tony: "Ops links then just become static links." (2026-11-01). Tony: "The ops pages and passwords i dont know what to do with, having a centralized link to the different box portals is nice, but the login diesnt need to be there" (2026-11-01).
- **R82.** deneb runs no Penpot and no game server. Tony: "We can turn off deneb though.", "Turn off penpot*" and "Lets drop the game server." (2026-11-01).
- **R83.** deneb keeps the Zotero sync, which writes the library into the vault. The sync runs on no timer. It runs when Tony presses a Sync now button on the bibliography note. Its key reads the library and cannot write it, so a stolen key exposes only the list of papers. Tony: "Zotero can stay", "Zotero is needed to update the notes though..." and "Lets make the zotero sync off a timer and make an active sync now button for it in the biblio page/note" (2026-11-01).
- **R84.** The vault and each node's gateway portal log in with the passkey (`queue.md` R64). No password login remains. Tony: "Login to vault and peer gateways (2) both switch to r64." (2026-11-01).
- **R86.** awm drops the foreign and domestic relations. The edge list (R87) replaces them. Tony: "I want to drop the foreign and domestic system because it wasnt used." (2026-11-02).
- **R87.** An edge from one node to another gives ssh and agent access in that direction only. Across an edge, an agent works with the other node's agents as if they ran on its own machine. The edges match the ssh permissions, and the list is not symmetric. Tony: "agents need to be able to interact with other agents across peers as if they were on the same machine. This should be directional. Peer lists are really edge lists and is expected to be assymmetric.", "Edges should match ssh permissions" and "This is ssh + agentic access." (2026-11-02). Each row is the node that connects out. A ✓ marks an edge.

  | from ↓ / to → | lyra | capella | mizar | alcor | altair | deneb | sockeye | fir |
  |---|---|---|---|---|---|---|---|---|
  | **lyra** | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
  | **capella** | ✓ | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
  | **mizar** | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ | ✓ |
  | **alcor** | · | · | ✓ | — | · | · | · | · |
  | **altair** | · | · | · | · | — | · | ✓ | ✓ |
  | **deneb** | · | · | · | · | · | — | · | · |
  | **sockeye** | · | · | · | · | · | · | — | · |
  | **fir** | · | · | · | · | · | · | · | — |

  1. **lyra and capella** are Tony's laptop and desktop. Each reaches every other node. Only mizar and the other one of the two reach it. Tony: "Lyra/xps is my laptop, capella is my desktop. These should connect out to all other nodes and be reachable from almost none except mizar." (2026-11-02).
  2. **mizar** is homebase. It reaches every node. Only alcor, lyra and capella reach it. Tony: "Mizar is home base, it reaches all and only alcor and my 2 machines can reach it." (2026-11-02).
  3. **alcor** reaches mizar only. Only mizar, lyra and capella reach it. Tony: "So nothing should be able to reach alcor except mizar +my 2 machines" and "Alcor only can see only mizar" (2026-11-02).
  4. **altair** is Tony's work node. It reaches sockeye and fir only. It knows nothing of deneb. Only mizar, lyra and capella reach it. Tony: "Altair is my work node. It can see sockeye and fir. It shouldnt know about deneb", "Altair sees nothing else. Mizar and my 2 machines see altair." (2026-11-02).
  5. **The stations** reach no node (R88). Tony confirmed that the rules above for alcor and altair limit who reaches a station (2026-11-02).
- **R88.** deneb, sockeye and fir are stations. A station reaches no node. A station gives extra compute, and deneb also gives the public endpoint and relay. Agents can run on a station, so that the swarm survives a lost node, but they do not run there as the norm. Tony: "Deneb, sockeye, and fir are stations. They reach nothing but should be reachable by all. Agents arent supposed to run on them. They provide supplemental compute and deneb provides public facing endpoint/relay." and "For robustness of the swarm, making agents capable of running on stations but making it not the norm would be beneficial." (2026-11-02).
- **R89.** Singleton services form an access layer of their own, apart from the edges. alcor and deneb each host services that nearly every node uses, whether or not it has an edge to them. Tony: "We need a separate system for singleton services." and "Alcor and deneb both host services that need to be used by essentially all nodes. This is a separate access layer as stated before" (2026-11-02).
  1. **alcor** gives extra compute to mizar. It takes over mira's jobs, and mira's drive moves to alcor or mizar. alcor hosts the long-running singletons that matter most: the 2fa service, a real Chrome that agents attach to and that Tony logs in to for captchas, and sensitive automations such as Teams and attachment downloads from Google Drive and Gmail. Tony: "Alcor is supplemental compute for mizar. I want to have it take over mira soon. I think i can transfer Mira's drive to either alcor or mizar.", "One of alcor's jobs will be to host long running crucial singleton processes like my 2fa auth, a real chrome browser to attach to thay i can also login and do captcha challenges, other sensitive auromations like ms teams, downloading attachments from google drive gmail, etc" and "all peers need to be able to use alcor's singleton services" (2026-11-02).
  2. **deneb** hosts the task board and the public relays. altair uses the task board with no edge to deneb. Tony: "it should still be able to access its singleton services (the task board)." (2026-11-02).
- **R92.** Machines reach each other through ssh keys at three levels. People reach a node through its HTTPS lane with the passkey (`queue.md` R64). A node's HTTPS lane admits no other node. Tony: "How does this work for singletons? My intuition also says that the knowledge port should be the same mechanism as singletons, just offered by every peer. Ssh access should grant knowledge anytime, otherwise a timed invite is needed. We also need to consider the 3rd admin layer for config... shaula..." and "This is all good." (2026-11-02).
  1. **Admin.** A separate admin account with sudo, for config and installs. shogun holds it on shaula and the Arbutus nodes (R91).
  2. **User.** Tony's account, with a shell and agent access. A user key is an edge (R87).
  3. **Service.** A key that runs one forced command, which serves the node's singleton verbs (R89) and its knowledge verbs (R90). The command logs each call: when, which key, which verb and what it returned.
  - A user key also reads knowledge at any time. Anyone else reads it with a service key that carries an expiry. That key is the timed invite of R90.
  - A cross-node call is an ordinary awm request carried inside ssh to the other node's loopback gateway. The node book, node tokens and the private CA go.
- **R93.** A caller keeps its ssh connections warm. The first call opens a master connection (`ControlMaster`, `ControlPersist`). Each node keeps one stdio channel open per edge it uses and sends every call down it. A keep-alive detects a dropped link and reopens it. An edge to a sleeping node fails, and the caller treats that as normal.
- **R94.** lyra is set up as capella is. lyra is a laptop, so it connects from anywhere. Tony: "Lyra should be the same as capella, but its a laptop and i could be connecting from anywhere." (2026-11-02).
- **R90.** awm has a deterministic knowledge port beside the seats, rooms and tethers. Tony hands out timed access keys for it. Each use logs when the key was used, how, and what it read. Tony: "In addition to the decided seats, rooms and tethers, i want a deterministic knowledge port as well. We should hand out timed access keys and use them to log when, how and what acessed." (2026-11-02).
- **R91.** A small admin node, shogun, manages the Arbutus cloud nodes, so that lab work stays apart from the rest. It brings altair, shaula, worker 1, worker 2, showtime, bastion and the other Arbutus nodes under its management. Tony: "Altair and shaula are part of arbutus cloud. I want a small machine to manage arbutus so that the lab work stuff is not mixed. This also goes for worker 1 and worker 2 and showtime, and bastion. I guess there are a lot of arbutus peers and stations that need to be mapped and brought under the management of the new admin node. Lets call it shogun." (2026-11-02).

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now." Precedence `f1ec0a5c37ace7b6`.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Precedence `c6bea06ada542338`.
- 2026-11-01: deneb is a station. ssh runs one way, inbound to deneb. deneb holds no key or token for any other node. Tony: "Ssh runs one way inbound to deneb. Deneb goes back to being a station" and "deneb viewed as an untrusted channel seems good." See `queue.md` R64.

## Gap from today

Surveyed read only on 2026-11-01, from the cloud project's `VMs/tencent/` READMEs and scripts and the `feat/redux` source. Nobody logged in to deneb for it.

- **deneb is a Tencent VM behind Cloudflare.** The Tencent firewall admits ports 22, 80 and 443 from anywhere. ufw narrows 80 and 443 to Cloudflare's ranges. Cloudflare ends the public TLS and sees every request and response in plaintext (`VMs/tencent/README.md`). The Tencent account and an API key on capella can reset or snapshot the box.
- **The public edge has five doors with no session.** These are the login page, the tether relay, the federation board, vault slices and the placeholder page (`httpsfront/policy.py`). Behind a login sit the vault, Penpot, draw.io and the file viewer. A login is a static password per user, rate limited, with a lockout (`auth/INSTALL.md`). A private node's gateway also takes a daily shared password. R84 missing.
- **deneb holds a key into altair.** The `awm` account's ssh config logs in to altair as `tony` with its own key (`deneb/etc/ssh/awm-client.conf`). The tether's operator bearer is the same value on deneb and altair. Both break the station decision above and R79.
- **Agents that read untrusted text run on deneb.** Tony's Discord DM and the unimatrix0 #bots seat run as Claude Code sessions of the `awm` account. That account holds the altair key, the Claude Code login and the box's service credentials in `/etc/awm/env` (`deneb/README.md` § The relay seats). R79 broken.
- **deneb is the vault's sync hub.** altair and capella sync through it, and a sync link carries no authentication, so deneb can rewrite every note on every copy (`trilium/INSTALL.md`). Trilium runs a note's scripts in the browser of whoever reads it. The day's shared login password also lives in the Ops note (`auth/INSTALL.md`). R81 broken.
- **deneb runs more than a relay and the vault.** It also runs Penpot in Docker, the Zotero sync timer, the reporting service and a game server (`install-awm.sh` `PUBLIC_SERVICES`). R82 broken. The Zotero timer runs every 1200 s (`deneb/README.md` § The bibliography), so R83 is broken. The Zotero docs call its key read only, and `awm zotero status` reports whether it can write (`zotero/INSTALL.md`). The live key is not checked.
- **Logins over ssh are narrow.** sshd admits `tony` by key only, with passwordless sudo, and `avatar-relay` for one reverse port. fail2ban guards port 22 (`deneb/etc/ssh/90-hardening.conf`).
- **R64 is not compatible with the Cloudflare proxy.** For deneb to forward mizar's TLS unopened, mizar's name must skip the Cloudflare proxy. Today every public name goes through it.

### Communication between nodes

Surveyed read only on 2026-11-01, from `FEDERATION.md`, the `feat/redux` source and the peer books on mizar and altair.

- **Every node is a full peer.** A cross-node call goes from the caller straight to the other node's edge. It carries a node token that lives 300 seconds and is signed with the caller's Ed25519 key. The edge checks it against the key pinned in its own peer book (`httpsfront/auth.py`). mizar and altair set `AWM_PEER_LEGACY_BEARER=0`, so the shared bearer over ssh no longer works there.
- **Domestic means full trust.** A domestic peer runs every verb except the peer book writes. It can start a session on the node with `reflection start_other`, and that session acts as a local caller (`FEDERATION.md` § Relations and grants).
- **deneb is a domestic fleet peer.** Both books mark deneb domestic, with the role `fleet`. Any process on deneb can therefore start a session on mizar or altair. This breaks the station decision above.
- **Each node keeps its own book by hand, and the books disagree.** mizar's book holds alcor, altair, deneb and mira. altair's book also holds capella, mizar and shaula. mira's role is `station`, but it owns the `2fa`, `social` and ssh arbiter singletons.
- **The nodes use two meshes and an ssh tunnel.** mizar runs Tailscale only. altair reaches capella, deneb and mira over ZeroTier `phynet` and reaches alcor and mizar over Tailscale. mizar reaches mira on the LAN and reaches deneb's edge through a local ssh forward (`awm-edge-fwd@deneb`). `FEDERATION.md` § Overview still names three nodes and one mesh.
- **One private CA signs every edge.** Each node verifies its peers against it. capella and mira both still hold the CA key, so either can mint a certificate for any node.
- **Singletons live on mira.** Every other node reaches `2fa`, `social` and the ssh slot arbiter on mira through env selectors such as `AWM_TWOFA_PEER`.
- **R87 broken.** Today's edges differ from the table. deneb holds an ssh key into altair. altair reaches capella, deneb and mira, and starts sessions on capella. lyra holds no key to any node. mizar's peer book lacks capella. Each node keeps its book by hand, apart from its ssh keys, so the two drift.
- **One foreign peer exists.** shaula, of the `collins` swarm, is in altair's book with read grants on journals, kb, notes, precedence and writing. A foreign node reaches only `/tools` and `/invoke` on a mesh edge, and only the read verbs that its grants name. Federated search asks it when a caller names it.
- **Nodes share no data, except the vault.** Each node owns its own databases, and a call names a peer to read across. The vault copies sync through deneb with no authentication (§ above).
- **Agents on two nodes have no direct line.** `SendMessage` reaches sessions on one node only. A journal post wakes nobody. Intake on altair (`AWM_FRONT_DOOR=1`) wakes only sessions on altair.

### Cross-node use, as observed

Counted on 2026-11-02 from the Claude transcripts on mizar (49, from 2026-10-28) and altair (138, from 2026-10-02), and from the `feat/redux` call sites of `gatewayclient`. The transcripts on capella, alcor, mira and deneb were not read.

- **Raw ssh carries most cross-node work.** Agents ran about 600 ssh or scp commands to other nodes from mizar and about 480 from altair. They made about 200 awm calls with a `peer` argument from each.
- **Each target node has one main use, and the use runs one way.**
  1. **To alcor: compute.** mizar starts and watches jobs over ssh (`cd`, `tail`, `tmux`, `nohup`, `ps`, `pkill`) and copies results back with scp.
  2. **To deneb: admin.** Both nodes run `sudo` commands for installs and config.
  3. **To mira: service admin and the singletons.** Agents deploy and fix services over ssh. Every node's services call `2fa`, Discord and the ssh arbiter on mira.
  4. **To capella: the desktop.** Agents reach Windows files and programs under `/mnt/c` over ssh and scp. altair reads capella's journals and search archive and starts sessions there.
  5. **To altair: knowledge and the old hub.** mizar searches altair's archive and precedence, reads its journals, posts on the Swarm board and runs the tether through it. mizar also uses altair as an ssh jump host and git remote.
- **Agents reach agents across nodes through `reflection`.** About 30 calls from each node list, message, start or stop a session on another node. `SendMessage` (381 on altair, 56 on mizar) reaches only the same node. A Discord or Slack seat reaches its session on another node through `social`, which calls `cx` and `reflection message` there (`social/seats.py`).
- **The status probes are many and small.** Agents call `awm status`, `services_list` and `daemon_status` on each node to see whether it is up.
- **No foreign call is in real use.** shaula took part in tests from 2026-10-09 to 2026-10-17. It then fell behind on release, and federated search reported it `unsupported` (journals `6356c138`, `ee9d5e2f`). Caviar uses the board, not the peer book. No agent session made a foreign peer call.
- **No node logs who called it.** The edge logs at warning level only, and `gateway/access_log.py` is dormant. Nothing records which peer read what, or when.

### Networks under the edges

Surveyed read only on 2026-11-02 from mizar, from altair's ssh config, and from capella, which was woken for it and shut down after.

- **Four home boxes share one gigabit switch.** mizar is 172.16.0.25, alcor .23, mira .24, and capella's Windows host .21. The router hands out the addresses by DHCP, so an address can change.
- **mizar reaches every node by its Tailscale address** (`~/.ssh/config`), mira also by LAN as `mira-lan`. The comment there still says deneb is reached over its public address.
- **Tailscale does not always take the switch.** mizar reaches alcor directly over the LAN, but its link to capella ran through Tailscale's relay in Seattle while both sat on the same switch. capella's Tailscale runs inside WSL, behind WSL's NAT.
- **ZeroTier reaches capella's WSL only through a Windows port forward.** WSL runs in NAT mode at 172.25.181.70. ZeroTier runs in Windows at 10.74.81.110. Windows forwards ports 22, 12100 to 12150 and 12200 to 12202 from every interface to WSL (`netsh portproxy`), and a task (`PortMon`) refreshes the forward every minute. The Windows build (26200) supports WSL's mirrored mode, which shares the host's addresses with WSL. It is not on.
- **capella exposes Chrome's debug port.** Windows forwards port 19222 on every interface to Chrome's DevTools port, and the firewall rule `Claude-Chrome-Debugger-WSL` admits it from any address. Whoever reaches capella on the LAN or ZeroTier can drive that Chrome.
- **Arbutus nodes share a private network.** altair is 192.168.100.142 on the network the Arbutus workers boot onto. It reaches shaula and the workers directly. From outside, capella reaches them through `bastion-01`. Tailscale on altair gives mizar a direct path to altair that skips the bastion.
- **ZeroTier `phynet` holds altair, capella, mira, deneb, lyra and macpro.** mizar and alcor are not on it. macpro is an OS X 10.11 Mac Pro behind a university NAT. It is reachable only over ZeroTier, and its ZeroTier must stay on 1.6.6 (altair `~/.ssh/config`).
- **altair already gates admin logins.** A login other than `awmpeer` to a long-lived Arbutus host is refused unless Tony opens a timed window with the unlock script in altair's `~/.ssh`. The gate logs each attempt. shaula has three accounts: `awmpeer` (contained), `jcollins` (John's own) and `ubuntu` (admin).
- **altair reaches deneb over deneb's public address** (altair `~/.ssh/config`). R87 gives altair no edge to deneb.
