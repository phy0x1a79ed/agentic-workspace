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

  | from ↓ / to → | lyra | capella | mizar | alcor | altair | deneb | sockeye | fir | macpro |
  |---|---|---|---|---|---|---|---|---|---|
  | **lyra** | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
  | **capella** | ✓ | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | · |
  | **mizar** | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ | ✓ | · |
  | **alcor** | · | · | ✓ | — | · | · | · | · | · |
  | **altair** | · | · | · | · | — | · | ✓ | ✓ | ✓ |
  | **deneb** | · | · | · | · | · | — | · | · | · |
  | **sockeye** | · | · | · | · | · | · | — | · | · |
  | **fir** | · | · | · | · | · | · | · | — | · |
  | **macpro** | · | · | · | · | ✓ | · | · | · | — |

  1. **lyra and capella** are Tony's laptop and desktop. Each reaches every other node. Only mizar and the other one of the two reach it. Tony: "Lyra/xps is my laptop, capella is my desktop. These should connect out to all other nodes and be reachable from almost none except mizar." (2026-11-02).
  2. **mizar** is homebase. It reaches every node. Only alcor, lyra and capella reach it. Tony: "Mizar is home base, it reaches all and only alcor and my 2 machines can reach it." (2026-11-02).
  3. **alcor** reaches mizar only. Only mizar, lyra and capella reach it. Tony: "So nothing should be able to reach alcor except mizar +my 2 machines" and "Alcor only can see only mizar" (2026-11-02).
  4. **altair** is Tony's work node. It reaches sockeye, fir and macpro only. It knows nothing of deneb. Only mizar, lyra and capella reach it. Tony: "Altair is my work node. It can see sockeye and fir. It shouldnt know about deneb", "Altair sees nothing else. Mizar and my 2 machines see altair." (2026-11-02).
  5. **The stations** reach no node (R88). Tony confirmed that the rules above for alcor and altair limit who reaches a station (2026-11-02).
  6. **macpro** runs a backup task. It and altair reach each other, and no other node reaches it. Tony: "macpro is running a backup task. 2 way edge with altair is sufficient" (2026-11-02).
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
- **R94.** lyra is set up as capella is. lyra is a laptop, so it connects from anywhere. Tony: "Lyra should be the same as capella, but its a laptop and i could be connecting from anywhere." (2026-11-02). R95 item 3 holds the one difference.
- **R95.** A Windows node runs its awm side in WSL with NAT networking. Mirrored mode stays off. Tony: "I want to avoid mirrored networking since this has had issues in the past an im not sure these issues were fixed." and "wsl shape is good. proposals accepted." (2026-11-02).
  1. WSL's ssh key and config are the node's one identity. A systemd path unit copies `~/.ssh` to the Windows profile on each change, so Windows ssh uses the same identity. The copy reacts to a change and does not poll. Tony: "I want to be able to directly ssh to all hosts as the wsl instance (there is an update_ssh manually triggered script for this, but ideally this becomes automatic on change without contant polling)."
  2. Inbound ssh lands in WSL. Windows forwards port 22 from its LAN address to `127.0.0.1:22`. WSL's localhost forwarding carries the connection into WSL at whatever address WSL holds, so nothing tracks that address. Tony: "Inbound to these should be to wsl."
  3. capella starts WSL at boot. lyra does not. lyra is a laptop and runs lean. Its agent access goes through a tether on the Windows side, for the Office and Zotero automation, and its WSL runs only when Tony starts it. Tony: "lyra (may not even be named correctly as old name was xps) doesnt start its wsl automatically. I typically just connected a tether to windows side for agentic access, which was mainly ms office + zotero etc automation" and "Id prefer for lyra to not start wsl automatically. it is a laptop so it  needs to run as lean as possible with minimal maintenance tasks."
- **R97.** Approvals run as a singleton service on alcor (R89). alcor signs an expiring grant for each request Tony approves with his passkey (`queue.md` R64, R65). alcor never acts on another node. Tony: "there are several approval mechanisms that are required now... I think it would be best if deneb hosted a page for this with a queue of pending approval events with a history of expired, approved, or cancelled." and "the approver is a singleton service that looks liek the same shape as 2fa, which is why I suggested alcor" (2026-11-02).
  1. Any node posts a request to the queue. deneb hosts the page that shows the pending requests and the history of approved, expired and cancelled ones. alcor keeps the log behind it.
  2. Every page that needs an approval embeds the same panel from alcor's address. deneb passes it through unopened. Tony: "would be great if we can have a single passthrough unit/modal for everywhere that needs it".
  3. The node that asked checks the grant itself with alcor's public key. An admin login (R92) uses a short-lived ssh certificate that alcor signs. Each node trusts alcor's key for its admin account, so no gate opens on the target.
- **R100.** Every admin login (R92), on any node, needs a grant from alcor (R97) that Tony approves on the queue page. A user login on an edge needs no approval, deneb included. Tony: "\"I don’t ssh to deneb without asking\" can we review this instruction?" and "2: lets make the request itself expire in 24hrs. the request can ask for an amount of time from 15(default as you said) to 24hrs" (2026-11-02).
  1. A request names the node, the reason and the length. The length runs from 15 minutes, the default, to 24 hours.
  2. A request expires 24 hours after it is made.
  3. The grant is a short-lived ssh certificate for the admin account. It lasts the approved length.
  4. One Ansible run asks for one grant, which covers every node in the run.
  5. Grants replace the ask-first rule for deneb and the hand-opened admin gates on altair and capella. The request is the ask, and its history is the log.
- **R101.** The normal account on every node, `tony` included, holds no sudo. Root work runs as the admin account under a grant (R100) or through Ansible. Tony: "tony and other normal usage users losing sudo seems to entirely depend on if we can use docker afterwards (like building metasmith or as a build image for rust, or vpn)." and "windows boxes are my dev machines Id like docker desktop to stay for those. We should still do the sudo/user split though" (2026-11-02).
  1. On the Linux nodes, the normal account uses rootless Docker and leaves the `docker` group. Builds, Rust images and the UBC VPN container run rootless. The VPN container runs on a network made with MTU 1400. Rootless Docker needs `slirp4netns`, because its fallback network driver accepts a published port but passes no data.
  2. Windows dev boxes keep Docker Desktop. Their WSL normal account still holds no sudo.
  3. The normal account reads system logs through the `systemd-journal` group.
  4. capella's awm runs as a user unit, so a deploy needs no sudo.
- **R102.** alcor's browser singleton (R89) gives each attached session its own window. A session attached to a window is a seat. Tony: "a real chrome browser to attach to thay i can also login and do captcha challenges", "agents attach to their own window so they can manage tabs?", "window per agent shape looks right" and "seat is just each attached session to a browser window" (2026-11-02).
  1. A seat opens, switches and closes the tabs in its own window. The browser verbs confine it to that window. No seat reaches the raw debug port.
  2. One viewer process shows every window. When a request needs Tony, alcor raises that seat's window first. Tony: "would be great if we can have 1 attachable viewer process rather than 1 per window seat".
  3. The resident site tabs, such as Teams, Slack, SharePoint and OneDrive, sit in one service window. Only the singleton drives them, one tab per site.
  4. The browser's services run in one container. It holds the shared Chrome with Tony's profile and the service window. A seat gets a window there by default. Tony: "lets do services all in 1 container, then per seat gets their own container (only when asked)" (2026-11-02).
  5. A seat that asks gets its own container. The container's type sets its profile: a copy of Tony's profile, or a blank one. Only a trusted session (R103) gets Tony's profile, in either form. A lab member's seat always gets a blank container. Tony: "type of container determines if it gets my credentials or a blank one", "the browser utility is still needed. so lab seats get the service but not my credentials." and "this can be tied to the access level -> only trusted sessions get my profiles through seats."
  6. A captcha or a sign-in goes through the approvals queue (R97). The request opens the viewer at that seat's window.
- **R103.** awm sets and tracks a trust level for each session, with the levels of `channels.md` R70. The level comes from what started the session: Tony, a seat's channel, or a room. An agent cannot raise its own level, and no message can raise it. A raise takes a grant that Tony approves (R97). Tony: "so trust level per session also needs to be set and tracked by awm so agents cant be coerced to elevate" (2026-11-02).
- **R98.** alcor has two ssh doors. The singleton door does not depend on mizar. Tony: "can we also force ssh through mizar, like how arbutus has bastion?", "The ssh access is not the same as the singleton service access right? machines that need 2fa dont have to have an edge into alcor. So the services should still work with mizar down, we just cant ssh into alcor with mizar down." and "2 ssh doors and singleton ports match what we discussed." (2026-11-02).
  1. Port 22 takes the user and admin logins (R92). alcor's firewall admits it from mizar only. capella and lyra jump through mizar.
  2. A second port takes the singleton calls. It is open to every node. It accepts only service keys, each locked to its command, with no shell and no forwarding.
- **R99.** alcor walls its singletons and its compute from each other, in both directions. A compute job finds it hard to affect a singleton. Tony: "Im thinking we have user level separation, but then compute also runs in bubblewrap. We need to make it hard from standard compute jobs on alcor to affect the services" and "is there any reason to block access the other way as well? services into alcor? if so, we bubblewrap both sides" (2026-11-02).
  1. Each singleton runs as its own system user under systemd's sandbox. It sees only its own files. A compromised singleton, such as the browser, cannot read a user's home or another singleton's secrets.
  2. Compute runs as a normal user inside bubblewrap. Its view leaves out the singletons' files. No normal user on alcor holds sudo or the `docker` group.
  3. The singletons together use at most 4 GB of memory, and less where they can. Each singleton has its own cap, so none grows without bound. The kernel reserves the singletons' memory, so compute is reclaimed first under pressure. The out-of-memory killer takes compute first, then the browser. 2fa and the auth services go last. Tony: "out-of-memory killer should be compute side only, maybe also browsers. the 2fa and important auth services should be the last to go down." (2026-11-02). Tony: "I also want to limit the services to at most 4GB of ram total, and much less if possible." and "this means memory watchers with some reserved space for teh services. Services should also not balloon ram without bound."
- **R96.** Each edge (R87) runs over one network. ZeroTier and Tailscale serve as tools where a route needs them, not as a standard. Tony: "We need to go over the networking between valid edges, choosing internet, lan, tailscale, or zero tier.", "Zerotier and tailscale are tools, not some standard i want to use." and "proposals accepted" (2026-11-02).
  1. mizar, alcor, mira and capella reach each other over the LAN, at addresses the router reserves. They share one gigabit switch. Windows reaches the LAN but not Tailscale, which runs inside WSL. Tony: "Lan may reassign ip." and "Alcor, mizar, mira, and capella are all physically plugged into the same gigabit switch so hopefully we can reduce hops there."
  2. A node outside Arbutus reaches the Arbutus nodes over the internet through `bastion-01` (`ProxyJump`). Tailscale comes off altair. Tony: "Arbutus machine access have to proxyjump through bastion from outside arbutus."
  3. A node reaches deneb at its public address. Tony: "we should set up deneb's networking properly, but if its just accounts and networking is fine, then sure, skip"
  4. sockeye and fir keep today's routes.
  5. lyra roams, so it uses Tailscale, in both directions.
  6. altair and macpro reach each other over ZeroTier. ZeroTier is macpro's only route.
- **R90.** awm has a deterministic knowledge port beside the seats, rooms and tethers. Tony hands out timed access keys for it. Each use logs when the key was used, how, and what it read. Tony: "In addition to the decided seats, rooms and tethers, i want a deterministic knowledge port as well. We should hand out timed access keys and use them to log when, how and what acessed." (2026-11-02).
  1. A timed invite grants read only or read and write. Either one opens the whole archive. Tony: "perms for timed invites is read only or read/write." (2026-11-02).
  2. The invite carries a short reason: roughly what the holder will search for, and what it means to write. The reason informs Tony and limits nothing. One search may lead to the next, and the log records each one. Tony: "should include wat they will search for and what tehy intend to write, roughly. just a simple justification. doesnt need to be strict, perms opens up the whole archive and searches may lead down to new searches which is fine and these are logged anyways"
  3. Tony approves each invite on the queue page (R97). The request shows its permission, its reason and its length.
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
- **R64 is not compatible with the Cloudflare proxy.** For deneb to forward alcor's TLS unopened, alcor's name must skip the Cloudflare proxy. Today every public name goes through it.

### Communication between nodes

Surveyed read only on 2026-11-01, from `FEDERATION.md`, the `feat/redux` source and the peer books on mizar and altair.

- **Every node is a full peer.** A cross-node call goes from the caller straight to the other node's edge. It carries a node token that lives 300 seconds and is signed with the caller's Ed25519 key. The edge checks it against the key pinned in its own peer book (`httpsfront/auth.py`). mizar and altair set `AWM_PEER_LEGACY_BEARER=0`, so the shared bearer over ssh no longer works there.
- **Domestic means full trust.** A domestic peer runs every verb except the peer book writes. It can start a session on the node with `reflection start_other`, and that session acts as a local caller (`FEDERATION.md` § Relations and grants).
- **deneb is a domestic fleet peer.** Both books mark deneb domestic, with the role `fleet`. Any process on deneb can therefore start a session on mizar or altair. This breaks the station decision above.
- **Each node keeps its own book by hand, and the books disagree.** mizar's book holds alcor, altair, deneb and mira. altair's book also holds capella, mizar and shaula. mira's role is `station`, but it owns the `2fa`, `social` and ssh arbiter singletons.
- **The nodes use two meshes and an ssh tunnel.** mizar runs Tailscale only. altair reaches capella, deneb and mira over ZeroTier `phynet` and reaches alcor and mizar over Tailscale. mizar reaches mira on the LAN and reaches deneb's edge through a local ssh forward (`awm-edge-fwd@deneb`). `FEDERATION.md` § Overview still names three nodes and one mesh.
- **One private CA signs every edge.** Each node verifies its peers against it. capella and mira both still hold the CA key, so either can mint a certificate for any node.
- **mira's browser uses 2.6 GB and escapes its memory cap.** Opera holds 12 tabs where 4 are needed. It runs as a snap, which moves it out of its unit's control group, so a memory cap on the unit has no effect. Gmail and Drive use IMAP and the REST API in the 90 MB social process, with no browser.
- **Singletons live on mira.** Every other node reaches `2fa`, `social` and the ssh slot arbiter on mira through env selectors such as `AWM_TWOFA_PEER`.
- **R87 broken.** Today's edges differ from the table. deneb holds an ssh key into altair. altair reaches capella, deneb and mira, and starts sessions on capella. lyra holds no key to any node. mizar's peer book lacks capella. Each node keeps its book by hand, apart from its ssh keys, so the two drift.
- **One foreign peer exists.** shaula, of the `collins` swarm, is in altair's book with read grants on journals, kb, notes, precedence and writing. A foreign node reaches only `/tools` and `/invoke` on a mesh edge, and only the read verbs that its grants name. Federated search asks it when a caller names it.
- **Nodes share no data, except the vault.** Each node owns its own databases, and a call names a peer to read across. The vault copies sync through deneb with no authentication (§ above).
- **Agents on two nodes have no direct line.** `SendMessage` reaches sessions on one node only. A journal post wakes nobody. Intake on altair (`AWM_FRONT_DOOR=1`) wakes only sessions on altair.

### Cross-node use, as observed

Counted on 2026-11-02 from the Claude transcripts on mizar (49, from 2026-10-28), altair (138, from 2026-10-02), alcor (1) and mira (6), and from the `feat/redux` call sites of `gatewayclient`. The transcripts on capella and deneb were not read. alcor's one session called only mizar, and mira's sessions reached only capella, over ssh.

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
- **Tailscale runs inside capella's WSL.** Windows on capella cannot reach a Tailscale address, so the Windows copy of capella's ssh config fails for every host it names by one.
- **ZeroTier reaches capella's WSL only through a Windows port forward.** WSL runs in NAT mode at 172.25.181.70. ZeroTier runs in Windows at 10.74.81.110. Windows forwards ports 22, 16122, 12100 to 12150, 12200 to 12202 and 19222 from every interface to WSL's fixed address (`netsh portproxy`). No task updates the forwards. They work only while WSL keeps that address. The task `PortMon-4231` samples port use for a Tcpip fault and does not touch them. The Windows build (26200) supports WSL's mirrored mode, which shares the host's addresses with WSL. It is not on.
- **capella exposes Chrome's debug port.** Windows forwards port 19222 on every interface to Chrome's DevTools port, and the firewall rule `Claude-Chrome-Debugger-WSL` admits it from any address. Whoever reaches capella on the LAN or ZeroTier can drive that Chrome. Close the port once the attachable browser on alcor runs (R89). Until then it stays open, so agents keep a dev browser. Tony: "For chrome dev, accept but wait until we implement all this before acting so we have access to the dev browser if needed in the meantime, which will become a singleton service. -> this will be the attachable browser on alcor." (2026-11-02).
- **capella answers on 172.16.0.21 and 172.16.0.22.** capella's ssh config still points `lyra` at .22, port 12022.
- **mira's network port runs at 100 Mb.** With all three home pairs busy at once, mizar to alcor fell to about 100 Mbit/s, likely from switch pause frames on mira's slow port. This is not proven (`~/handoff/net/README.md` on mizar).
- **altair's browser service drives capella's Chrome** through the debug port 19222.
- **Arbutus nodes share a private network.** altair is 192.168.100.142 on the network the Arbutus workers boot onto. It reaches shaula and the workers directly. From outside, capella reaches them through `bastion-01`. Tailscale on altair gives mizar a direct path to altair that skips the bastion.
- **ZeroTier `phynet` holds altair, capella, mira, deneb, lyra and macpro.** mizar and alcor are not on it. macpro is an OS X 10.11 Mac Pro behind a university NAT. It is reachable only over ZeroTier, and its ZeroTier must stay on 1.6.6 (altair `~/.ssh/config`).
- **altair already gates admin logins.** A login other than `awmpeer` to a long-lived Arbutus host is refused unless Tony opens a timed window with the unlock script in altair's `~/.ssh`. The gate logs each attempt. shaula has three accounts: `awmpeer` (contained), `jcollins` (John's own) and `ubuntu` (admin).
- **altair reaches deneb over deneb's public address** (altair `~/.ssh/config`). R87 gives altair no edge to deneb.
- **The Arbutus project holds more nodes than the fleet knows.** The arbutus-infra Terraform (altair, `dev` worktree) defines `bastion-01`, `worker-01` and `worker-02`, altair (devbox-02), a devbox, the GTDB-Tk gateway and its on-demand workers, showtime, a services map, `macbu-stage` and the `awm-peer` security group that holds shaula. The live list was not read from OpenStack.

### Test of the edges (R95, R96)

Run on 2026-11-02 from mizar and capella.

- **The LAN and Tailscale take the same time between home boxes.** Tailscale now goes direct over the switch, at about 1 ms per ping. A fresh ssh login costs 230 to 470 ms on either network. Over one held connection, a call costs 20 to 42 ms.
- **The localhost forward works.** A forward from capella's LAN address to `127.0.0.1:22` reached WSL from mizar at about 400 ms per fresh login. It kept working after capella slept and woke. capella answered 13 s after the wake packet.
- **The `~/.ssh` watcher works.** It runs on capella as `ssh-to-windows.path`. Windows ssh with the copied identity reached mizar and alcor by their LAN addresses.
- **alcor holds no ssh key.** The edge from alcor to mizar does not exist yet.
- **capella reaches `bastion-01` with no Duo prompt.** capella's admin gate refuses the jump on to altair as `tony` until Tony opens a window. The `awmpeer` key is refused by altair.
- **deneb answers ssh on its public address and on Tailscale.** Tailscale reaches it through the public address, at 34 ms.

### Test of rootless Docker (R101)

Run on 2026-11-02 on mizar and in capella's WSL. Docker Desktop on capella was left unchanged.

- **Builds work rootless.** hello-world, a Rust release build and the UBC VPN image built and ran. Files written into a mounted folder belong to the user.
- **The real VPN connect works rootless.** On capella the tunnel came up 11 s after the dial, with UBC's exit address. One `hostname` on sockeye answered through the bounce. Two Duo pushes were used, and mira's 2fa approved both.
- **capella's sockeye path sends an unapproved push.** `awm-sockeye-up.sh` cannot arm mira's 2fa from capella, because `awm 2fa` is no command there and the local fallback returns 404. A plain `awm-ssh sockeye` from capella sends a Duo push that nothing approves, which counts toward the lockout.
- **Only memory and pids limits work rootless.** The user session delegates no `cpu` or `io` controller, so CPU and disk limits are dropped.

### Pilot of warm ssh (R93)

Measured on 2026-11-02 from mizar to alcor. Each call is a `GET /tools?view=domains` on alcor's loopback gateway. That request alone takes about 25 ms on the far gateway.

| Path | Tailscale median | LAN median |
|---|---|---|
| Fresh ssh connection per call | 463 ms | 461 ms |
| `ControlMaster`, new channel per call | 65 ms | 44 ms |
| Held tunnel, new TCP per request | 56 ms | 38 ms |
| Held tunnel, keep-alive request | 28 ms | 24 ms |
| Today: HTTPS edge with a node token, new TLS per call | 35 ms | — |

- A held connection with keep-alive adds almost nothing to the far gateway's own time. It beats today's HTTPS call by about 7 ms.
- A fresh ssh connection costs about 440 ms, so R93's warm connection is needed.
- The LAN beats Tailscale by 4 to 20 ms per call, even though Tailscale takes a direct path to alcor.
