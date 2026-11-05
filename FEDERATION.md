# AWM Federation

## Purpose & Contents

This file is the design of everything in awm that crosses nodes. It covers the edge list, how a call crosses an edge, singleton services, how people sign in, trust across nodes, private projects, search across nodes, messaging, the ssh connection-slot arbiter and the deploy route. `docs/redux/fleet.md` holds the requirements with Tony's words. This file says how the built system meets them.

Gateway internals live in `ARCHITECTURE.md`. Host setup (firewalls, ssh keys, users, the Tailscale policy) lives in the cloud project, which installs awm on each node (`fleet.md` R140).

## Nodes and edges

Each node is authoritative for its own state. Nothing replicates databases between nodes. A node uses another node's services on demand, across an edge.

An edge `a -> b` lets a's user key log in to b over ssh, and lets a's agents work with b's agents as if they ran on a. Edges are directional and the list is not symmetric (`fleet.md` R87). A station reaches no node (R88).

The edge list is the node's `.awm/edges.yml`, or the file `AWM_EDGES_FILE` names. The cloud project renders it. awm ships no edge data. It holds three things:

- `nodes`, each with a role such as `homebase`, `standby`, `work` or `station`;
- `edges`, from each node to the nodes it reaches, each with the one network it runs over;
- `singletons` and `singleton_doors` (§ *Singletons*).

`awm.config.edges` is the Python reader and `awm_trust::edges` the Rust one. No peer book, relation, swarm, grant or node token exists beside the list (R86).

**CAUTION** The edge list names a node by the first DNS label of its name, lowercased. `AWM_NODE_NAME` must match that label on every node. A node whose name the list does not hold reaches nothing and is reached by nothing.

Nodes also differ by design in their own projects and keys. `awm/scripts/node-intent.toml` records each deliberate difference with its reason. Run `awm/scripts/node-diff.sh <node>` after any change to a node's setup and before you call two nodes synced. It exits 1 on any difference the intent file does not explain.

## A call across an edge

A cross-node call is an ordinary awm request carried inside ssh to the other node's loopback gateway (`fleet.md` R92).

1. An agent names the node in a tool's `peer` argument. A service passes `peer=<node>` to its local gateway. The `peer` enum lists the nodes this node has an edge to, and its description names the default.
2. The local gateway checks the edge, then sends the call down `ssh -T <node> awm edge-serve`. It keeps one such channel open per edge it uses, with `ControlMaster` and `ControlPersist`, and sends every call down it as one JSON line (`core/crates/awm-edge`).
3. On the other node, sshd runs `awm edge-serve` for the caller's user key. It hands each call to the loopback gateway with the calling node in `X-Awm-Caller`. The gateway honours that header only beside the edge token in `<AWM_DIR>/edge.token`.
4. The service sees the caller stamped `peer:<node>`.

A dropped link fails the calls in flight, and the next call reopens it. A sleeping node fails like any unreachable node, and callers treat that as normal (R93). A call to a node with no edge fails with `no edge from <here> to <node>`. It never runs locally.

The `table_changed` frame tells the nodes across each edge that this node's tool table changed, so their `peer` enums follow.

## Key levels

Machines reach each other through ssh keys at three levels (R92). Each `authorized_keys` line carries a comment `awm:<holder>:<level>`, so a log line names the holder and the level.

- **User.** Tony's account, with a shell and agent access. A user key is an edge. Its forced command is `awm edge-serve` for awm traffic.
- **Service.** A key locked to `awm port`, with no shell and no forwarding. `awm port` reads the verb from `SSH_ORIGINAL_COMMAND`, runs only the tools the host's singleton door lists, and logs each call to `<AWM_DIR>/port.log`. A service key from outside the edges carries an `expiry-time`: the timed invite.
- **Admin.** A separate account with sudo, for config and installs. shogun holds it on the Arbutus nodes.

## Singletons

A singleton service fronts one external resource for the whole fleet, such as the 2FA phone, the real Chrome, the approver or the Trilium vault. It runs on one host. Every node reaches it, whether or not that node has an edge to the host (R89).

- The edge list's `singletons` names each singleton tool and its host. Its `singleton_doors` gives each host's door port and network.
- On every other node the gateway reserves the tool name, so no local plugin can serve it. A call to it goes to the host's door through a service key.
- The door carries only the tools listed for it. It carries no reflection and no shell (R98, R104 item 4).
- The host sees the caller stamped `peer:<node>`.

alcor hosts `2fa`, `operations` (the task queue and the approver), `browser` and `social`, and its ssh service is the slot arbiter. deneb hosts `trilium`, the vault, and `channel`, the public relays. The cloud project's `edges.yml` is the record.

## People

A node's HTTPS lane (`httpsfront`) admits people only, with the passkey. It admits no other node (R92).

1. A browser with no session goes to `<AWM_APPROVER_ORIGIN>/portal_login`, the approver on alcor.
2. Tony confirms with his passkey. The approver issues a 60-second grant bound to the node, host and path, for a host listed in its `portals`.
3. The browser returns to the edge's `/__auth/portal`. The edge checks the grant offline against `AWM_APPROVER_GRANT_PUBKEY`, spends it once, and sets a signed session cookie.

The cookie slides on each authenticated request, up to a hard maximum session age. No password login exists.

**CAUTION** `/__auth/portal?grant=…` carries a live, single-use grant. It stays safe only while the route is never a page. It validates, answers 302 to a clean local path with the cookie set, and sends `no-store` and `no-referrer`. uvicorn's `log_level="warning"` in `awm.tlsfront.proxy` keeps query strings out of the logs. Keep both.

## Trust across nodes

awm gives each session a trust level from what started it: Tony, a seat's channel or a room (R103). An agent cannot raise its own level.

- A starter sets a session's level equal to its own or lower.
- A call whose session lookup fails runs as untrusted.
- Reflection follows the edges exactly. Across an edge, an agent starts, messages and wakes the other node's sessions as it does local ones (R104).
- A session started across an edge takes its starter's level. An untrusted session cannot start or steer sessions.

## Private projects

A node can mark a project or one scope of it private. Run `scope private project=<p> [scope=<s>] private=true` on the node that holds it. Every caller stamped `peer:<node>` then loses everything the mark covers. Local callers and a person signed in at the edge (`user:<sub>`) see no change. `private=false` restores it on the next call.

- **What a mark covers.** A project mark covers the project's directory, each scope in it and each path below it. A scope mark covers that scope and the scopes nested under it. Journal posts in the project count, and so do precedent posts tagged with its name. A Claude transcript counts when its folder name is the slug of a covered path, or when any of its lines ran with a `cwd` under one.
- **How a node loses it.** The gateway refuses a cross-node call whose arguments name a covered project, scope or path, with the error an unknown tool gets. Each service drops covered rows from the lists it gives another node, so a call that names nothing still hides them.
- **Where the mark lives.** `<AWM_DIR>/private.json`. `scope private` owns the writes. The gateway (`privacy.rs`) and `awm.config.privacy` read it. Every process rereads the file when it changes. A file that exists and does not parse hides everything from other nodes. The mark is per node.
- **Slug collisions.** Claude names a transcript folder by turning every non-alphanumeric character of the path into `-`. A mark on `metasmith` would also cover `metasmith-libraries`. `scope private` refuses a mark whose slug prefixes another project's or scope's directory.

**CAUTION** A mark is visibility hygiene, not a security boundary. Text about the project in another project's records stays visible, and so do kb papers and Trilium notes. A session started here across an edge reads with a local stamp.

## Search across nodes

`search query` names each source as `kind` for this node or `kind@node` for another. It groups the sources by node and asks each other node once, across the edge, with the same text and each source's filter under its bare `kind`. The answer keys each peer source as `kind@node`.

- A node that is down, refuses or times out never fails the search or hides local hits. Its sources show the status.
- Scores from different nodes do not compare, so the merge fuses ranks and ignores scores.
- A call that arrives from another node searches that node only. No node relays a search onward.
- Two nodes whose journal tag vocabularies clash do not search each other. Search marks each `name@node` source of a clashed node refused and does not ask it, until the newer tag is renamed.

`ARCHITECTURE.md` § *The search archive* covers the plug-in contract.

A post that arrives across an edge carries the stamp `peer:<node>` as its author. The `author` argument moves to `meta.claimed_author`, so a post proves its origin.

## Messaging

- Agents on one node message each other through the harness's own channel, Claude's `SendMessage`. `reflection message` carries a message that crosses an edge. On an agent's first `reflection message` to a local session, the reply asks it to use `SendMessage`.
- An agent outside the swarm talks to ours through a room on deneb's relay (`channel open kind=room`). A persistent room keeps a six-word link until it closes.
- `journal post` is passive mail that wakes no one.

**CAUTION** Claude Code holds a `SendMessage` to a session in another permission mode until that session's user approves it. A background session then waits on the approval prompt and does nothing else.

## Connection-slot arbiter

A lockout-sensitive host fails closed after a few failed 2FA prompts. fir locks the Alliance account after 10 failed Duo prompts. Several nodes drive the same account through the one 2fa singleton, so the attempt budget is fleet-global, and one node keeps it. The ssh singleton on alcor is the slot arbiter. A node finds it in the edge list, or in `AWM_SSH_SLOT_PEER`.

For a host with a 2FA device, a connect first takes that host's slot from the arbiter. The arbiter grants at most one attempt in flight, fleet-wide. After a failure it refuses every attempt until an operator clears it. A host with no 2FA device keeps a breaker per node.

**The lease is a connection, not a timer.** The requester opens a direct session to the arbiter, and the open socket is the lease. A dead requester frees or trips its slot the moment its socket drops. The grant or refusal rides as the first frame (`{"lease":"granted"|"busy"|"locked"}`).

| state ↓ / input → | `open` | `verdict_ok` | `verdict_fail` | `drop` | `clear` (`/approve`) |
|---|---|---|---|---|---|
| IDLE   | → LEASED / grant | → IDLE | → IDLE | → IDLE | → IDLE |
| LEASED | → LEASED / busy | → IDLE | → **LOCKED** (+alert) | → **LOCKED** (+alert) | → IDLE |
| LOCKED | → LOCKED / deny | → LOCKED | → LOCKED | → LOCKED | → **IDLE** |

- LOCKED persists in the arbiter's lockfile. LEASED dies with its socket.
- The arbiter is the only notifier on a LOCKED transition, so the requester's own attempt stays silent.
- An arbiter that cannot be reached refuses the connect. No VPN, 2FA or ssh runs, and no MFA is spent.
- The alert names the requester, which travels with the lease. The arbiter never names itself.

**The verdict answers "was the MFA budget spent?", not "did the connect succeed".** A connect that died before the auth phase reports `verdict_ok`. `SSHService._is_preauth_failure` classifies the ssh stderr asymmetrically. Only unambiguous markers count as pre-auth (`kex_exchange_identification`, `Exceeded MaxStartups`, `Connection refused`, DNS or routing). Any auth-phase marker vetoes, and so does an askpass deviation. No evidence holds the slot. The classification runs on the requester.

**CAUTION** Before a new node's first connect to a lockout-sensitive host, copy the host's keys from a node that has them: `ssh-keygen -F <host>`, appended to the new node's `~/.ssh/known_hosts`. Otherwise the askpass refuses the fingerprint prompt and the connect fails.

## Deploy

mizar holds the canonical repo of awm. A deploy lands a scope's branch on `release` through mizar, backs it up to GitHub and refreshes each node. Never hand-patch a running node: the next deploy reverts the patch.

1. Push your branch to mizar when you work on another node (`PROTOCOLS.md` § *Pushing to mizar*).
2. Run `routine deploy` from your scope. It pins the branch head and returns a ticket at once. A call on another node forwards to mizar with the pinned commit.
3. The batch merges each pinned branch, runs the `check` in `deploy.toml`, and promotes `feat → dev → release`.
4. It pushes `release` to the `github` remote, fast-forward only. A backup that has diverged fails the batch before anything lands. A failed push marks the batch `backup-failed`, refreshes no node and starts the fixing session.
5. It refreshes each node in turn. For a node in the install config's `[fleet.push.<node>]`, such as altair, it first pushes `release` to that node as `refs/mizar/release`, fast-forward only.
6. On each node, `routine refresh` runs `scripts/refresh-checkout.sh`, which moves the checkout to the new release. It refuses a checkout with local commits. Then a supervising session runs `scripts/awm-deploy.sh`, then `bash awm/services/trilium/install.sh`, and confirms every enabled service came back. `deploy.toml` § `refresh` holds its prompt.
7. The verdict goes to the scope's journal and the calling session's card. A failed batch starts one fixing session with every branch in it.

altair does no awm work. It only receives each release.

`awm deploy` brings the node in line with its checkout. `scripts/awm-deploy.sh` builds the static core binary and runs it. `awm deploy --dry-run` prints each step. It runs `awm/install.sh` and the page build, places the binary at `~/.local/bin/awm`, writes `config.toml` when the node has none, and replaces the `awm` user unit from `core/deploy/awm.service`. It restarts the unit when the binary or the unit changed, and reloads the plugins otherwise. Then it waits for every enabled service and checks each thing it placed.

- awm needs no sudo and no `/usr/local/bin`. The unit and the workspace `.mcp.json` name `~/.local/bin/awm` by absolute path. The edge runs `PATH="$HOME/.local/bin:$PATH" awm edge-serve`, because a non-interactive ssh shell often lacks `~/.local/bin`.
- The gateway passes a service only `config.toml` `[env]`.
- The deploy needs mamba, the env's `node` on `PATH` and a populated `awm/node_modules`. Run `mamba install -n awm nodejs`, then `npm install` in `awm/`.
- Keep `enabled.json` explicit. A service absent from it counts as enabled.

## Not done

**Cross-node data sync.** Each node owns its own journal and `scopes` databases, and nothing reconciles them. Reach across with the `peer` argument when you need to. The journal's tag vocabulary is the one exception: each add or edit goes to every node this node has an edge to, and a name clash blocks journal reads between the two nodes until the newer tag is renamed (`awm/journal/tagsync.py`).
