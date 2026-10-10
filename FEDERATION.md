# AWM Federation v1

## Purpose & Contents

This file is the design of awm federation. It covers how nodes reach each other, how a peer proves who it is, what a foreign swarm may read, and how swarms exchange work. The sections run in this order: the node roles and the shape of a cross-node call, authentication, node tokens, relations and grants, the foreign gate, archive search, the board and the front door, the legacy SSH bearer, the peer directory, the cross-peer transports, singletons, the connection-slot arbiter, TLS and deployment.

Operator steps for the board and the front door live in each service's `INSTALL.md`. Gateway internals live in `ARCHITECTURE.md`.

## Overview

Cross-node networking so every host in the fleet is a full peer AWM node. Nodes use
each other's services **on demand** — defaulting to local, **never syncing
databases, never relaying peer traffic through a gateway** — behind a **single
authentication system** while the loopback interior stays open.

| Node | Where | Role |
|---|---|---|
| `capella` | WSL on a Windows desktop | holds the **CA key**; sleeps with the host |
| `mira` | always-on mini PC | owns the singletons: `2fa`, `social`, the ssh slot arbiter |
| `altair` | Arbutus cloud VM (16 vCPU / 1.9 TB) | always-on compute; borrows every singleton from mira |

All three are reached over the ZeroTier mesh `phynet` (`10.74.81.0/24`); the `*z`
ssh aliases mean "same host via the mesh". altair is private-only on its cloud
network, so the mesh is its *only* route to the other two.

This is deliberately **not** the retired federation (cr-sqlite replication,
leader election, a peers/replication registry). It is point-to-point service
composition: each node is authoritative for its own state; peers are visible on
demand; there is no single identity brain (per-node state, cross-reads on demand).

## The shape

```
   agent / CLI / awm-mcp                      agent / CLI / awm-mcp
          │ loopback (open, no auth)                 │
   ┌──────▼───────┐   peer_resolve(name)      ┌──────▼───────┐
   │  gateway A   │◄───(directory only)       │  gateway B   │
   │  :7819 loop  │                           │  :7819 loop  │
   └──────┬───────┘                           └──────┬───────┘
          │ fronts                                   │ fronts
   ┌──────▼───────┐   TLS + node token         ┌─────▼────────┐
   │ httpsfront A │──────────────────────────► │ httpsfront B │  (edge auth)
   │  :12100 edge │   direct, CA-verified      │  :12100 edge │
   └──────────────┘                            └──────────────┘
```

- **Service calls default to the local gateway.** A call runs on a peer only
  when the caller names it in the `peer` argument, or when that peer is the
  domain's default provider.
- **The gateway is a resolver, never a relay.** A cross-peer call asks its *own*
  gateway to resolve the peer's edge address (`peer_resolve`), then talks to the
  **peer's edge directly** — no peer bytes traverse a gateway.
- **Auth is edge-only.** The loopback gateway (`:7819`) stays open and
  auth-unaware; local CLI / `awm-mcp` never authenticate. The `httpsfront` edge
  (`:12100`) is the single authenticated door. The board mount at `/board/` is
  the one exception: it answers before edge authentication, because the board
  authenticates its own parties.

## Components (v1)

| Piece | Where | Role |
|---|---|---|
| `auth` service | `awm/services/auth/` | credential **authority**: mints paired (login-password, legacy peer-credential) generations, signs sliding session tokens, holds the node's Ed25519 key and signs node tokens, pushes the day's password to Discord, mirrors the peer credential to `$AWM_PEER_CRED` |
| `httpsfront` edge | `awm/services/httpsfront/` | **enforces** auth at the network edge: session cookie (sliding), node token OR legacy bearer, CA-verified TLS, login page / 401. It stamps `X-Awm-As`, serves the landing page at `/` and mounts the board door |
| peer directory | gateway (`awm/gateway/awm/gateway/peers.py`) | the address book and trust record: `peer_add`/`peer_join`/`peer_set`/`peer_grant`/`peer_revoke`/`peer_list`/`peer_resolve`/`peer_forget` |
| peer capabilities | gateway (`awm/gateway/awm/gateway/peer_catalog.py`) | which domestic peer provides which MCP domain, and the default provider per domain: `peer_providers` / `providersOf` |
| foreign gate | gateway (`awm/gateway/awm/gateway/catalog.py`) | which verbs a foreign caller may run |
| cross-peer calls | `gatewayclient.call_peer` + the `awm-mcp` proxy | reach a peer's edge directly (client-side federation) |
| board | `awm/services/board/` | the swarms' shared cards, stored in the Trilium vault |
| front door | `awm/services/representative/` (domain `door`) | queues the swarm's cards and keeps the representative alive |

## Authentication

**Paired, co-rotated, distinct values.** Every ~12 h the `auth` service mints a
fresh pair — a `login-password` (human, typed once a day) and a `peer-credential`
(machine, distinct value, never pushed to Discord) — each valid ~24 h. With a 12 h
cadence and 24 h validity, **two generations are valid at once**, so a client
never has to re-authenticate across a rotation. On startup it mints if none is
valid or the newest is older than the cadence (no dependency on `events`).

Each mint: a **loud** log line, a best-effort push of the *login password* to
the operator's Discord DM, and a rewrite of the `$AWM_PEER_CRED` file.

- **Human login:** `POST /__auth/login` at the edge → `auth.verify` → a
  signed session cookie. The cookie is **slid** (re-issued) on each authenticated
  request within its window, capped by a hard maximum session age. Get the day's
  password on the daemon host with `awm auth password` (it is also in Discord).
- **Peer auth:** a peer sends `Authorization: Bearer <node token>` (§ *Node
  identity and tokens*). Until the legacy bearer retires, a peer may send
  `Authorization: Bearer <peer-credential>` instead, and the edge checks it
  against the currently-valid peer credentials.

The edge validates cookies **offline** using a signing secret it fetches once
from `auth.edge_material` (refreshed every ~30 s to pick up rotated peer creds,
pinned peer keys and relation changes),
so there is no auth RPC on the hot path. Fail-closed: if `auth` is unreachable
and no material is cached, the edge authenticates nothing.

## Node identity and tokens

Each node holds an Ed25519 key. The `auth` service mints it on first start and keeps it at `$AWM_DIR/services/auth/node_ed25519.key`. The `auth` verb `node_key` returns the public key and its fingerprint. The verbs `sign_peer_token` and `edge_material` refuse any caller that carries an identity stamp, so only a local process can ask for a signature.

A node token is `awmpt1.<claims>.<signature>`. The claims are `iss` (the signing node), `aud` (the target node), `iat` and `exp`. A token lives 300 seconds. A caller reuses one until 30 seconds before it expires. The code is `awm.config.peertoken`.

To pair two nodes, exchange the public keys over a channel you trust and compare the fingerprints. Pin each key in the other node's book with `awm peer set <name> --public-key <key>`. The edge verifies a token only against the key pinned for its issuer.

**CAUTION** The `aud` of a token is the first DNS label of the target's `node_name()`, lowercased. The target compares it with its own `node_name()`. The peer book name must therefore equal the target's `node_name()`. A mismatch fails every token with 401. A domestic peer then falls back to the legacy bearer, so the fault stays hidden until the bearer retires. The caller logs the `aud` it signed for on each refusal.

For each request the edge:

1. Verify the token's signature, `aud` and expiry against the pinned key.
2. Strip inbound `X-Awm-As`, `X-Awm-Caller-Pid`, `X-Awm-Session-Pid` and `X-Awm-Peer-Redirect`.
3. Stamp `X-Awm-As: peer:<node>`, where `<node>` is the peer book name.
4. Remove the token from the forwarded request.

The legacy bearer authenticates as the bare `peer`, which the gateway treats as a domestic peer. Set `AWM_PEER_LEGACY_BEARER=0` in `<workspace>/.awm/env` to retire it. The edge then accepts only node tokens, and `auth` stops handing the bearers to the edge.

The edge caches the auth material for up to 30 seconds (`httpsfront/auth.py` `_REFRESH_INTERVAL`). The material holds each peer's relation and pinned key. A new key or a relation change reaches the edge within that window. The gateway reads the peer book on every call, so a grant change applies on the next call.

## Relations and grants

Each peer book entry (`<AWM_DIR>/state/peers.json`) holds the edge URL, the ssh alias, the relation, the swarm, the principal, the role, the pinned public key with its fingerprint, and the grants. `gateway/peers.py` owns the writes. `awm.config.peerbook` is the only reader. An entry written before relations existed reads as domestic.

- **relation.** `domestic` marks a peer of this swarm and trusts it with every verb except the peer-book writes (`peer add|join|set|grant|revoke|forget`) and the `auth` verbs `sign_peer_token` and `edge_material`, which refuse every stamp. `cx start` and `cx stop` refuse the legacy bare `peer`, which names no node. `foreign` marks a peer of another swarm. A foreign peer needs a swarm other than this node's swarm.
- **swarm and principal.** The swarm names the peer's swarm. The principal names the person the peer acts for.
- **role.** `fleet` or `station`.
- **grants.** The read categories a foreign peer may use. The categories today are `journals` and `kb`. A grant on a domestic peer is stored and changes nothing.

Two environment variables describe this node itself:

- `AWM_NODE_ROLE` is `fleet` (default) or `station`. A station hosts services for others and runs no agent sessions. `cx start` and the front door refuse on a station, and the gateway logs a warning at boot when a station has `cx` enabled. Any other value is an error.
- `AWM_SWARM` names this node's swarm (default `tony`). The front door claims cards for it, and archive hits carry it as `origin_swarm`.

Manage entries with `awm peer add|set|grant|revoke|list|resolve|forget` or the `peer` domain, which carries the same verbs. The peer-book writes close to a caller with a peer stamp. Each write refuses `peer` and `peer:<node>` on every door, the HTTP routes included. A domestic peer is refused as much as a foreign one. The refusal is a `PermissionError`, which `/invoke` and the routes answer with 403. A foreign caller never reaches it, because the foreign gate answers 404 first. The operator CLI, a local agent and a person signed in at the edge (`user:<sub>`, including `user:operator`) pass. The writes also declare `effect = write`, so a restricted session cannot run them. **CAUTION** Domestic trust is full trust. A domestic peer can start a session here with `cx start`, and that session's agent carries a local stamp. Mark a node domestic only if you control it.

## The foreign gate

A caller is foreign when the edge stamps it `peer:<node>` and the peer book does not call `<node>` domestic. A node missing from the book is foreign with no grants. A domestic peer and the legacy bare `peer` are not gated by this section.

1. **Paths.** A foreign node reaches `/tools` and `/invoke` on a mesh edge and nothing else (`policy.FOREIGN_PATHS`). WebSockets, emit topics, `/files`, `/svc`, the hub, the vault and the landing page answer 404. `/tools` refuses the `peers` view. The public profile refuses `/invoke` for everyone.
2. **Verbs.** A foreign caller runs a verb only if the verb declares `effect = read` and a `category` that the peer's `grants` list. The gateway enforces this in `catalog.dispatch`. `/tools` and `describe` show the same cut catalog.
3. **Refusals.** Every refusal is the error an unknown tool gets, which the edge returns as 404. This includes `providersOf`, a `peer` argument (a foreign node gets no onward hop through this node) and a `PermissionError` raised inside a service. A foreign peer learns nothing about what exists.
4. **Backstop.** The `/svc` proxy refuses a foreign caller on every path.

Every verb declares an effect: `read`, `queue`, `write` or `secret`. A verb with no declaration counts as `write`. Only the journal reads in `scopes` carry the category `journals` and only the `kb` reads carry `kb`. Every other read verb has no category, so no grant reaches it until someone adds one.

A category is the only boundary. A read verb that returns journal text under another category leaks around the grant. Review each new read verb for this. Services also narrow what a granted category returns:

- `scope_fetch` shows a foreign peer journal posts only.
- `kb recall` gives a foreign peer only the hybrid, vector and lexical modes. Posts need the `journals` grant on top of `kb`.

Revoke a grant with `awm peer revoke <name> <category>`. The next call is refused.

## Archive search

`scope archive_search` searches journals across the federation. It runs this node's search and asks every peer in the book at once, with the local-only verb `scope_fetch` and `kind=journal`. Each peer answers through its own gate, so a foreign peer answers only if it granted this node `journals`. Each hit carries `origin_swarm` and `origin_node`. The reply also carries a `peers` map with `ok` or an error for each peer asked. A peer that is down, refuses, times out (10 seconds) or sends more than 2 MiB never fails the search.

Scores from different nodes do not compare, so the merge interleaves hits by rank within each source. A call that arrives from a peer searches this node only. A second fan-out here would relay our peers' journals to someone they never granted.

A post that arrives through the edge carries the edge's stamp (`peer:<node>`, or bare `peer` for the legacy bearer) as its author. The `author` argument moves to `meta.claimed_author`. A post therefore proves its origin.

## The board

The board is the swarms' shared place to post requests and messages. Every party connects out to it, so no swarm opens a port for another. A **card** is a request, which a swarm can claim, complete or fail, or a message, which nobody claims. A reply is a new message card with `reply_to`. The first release carries requests and messages only.

- **Store.** A card is a child note of the Trilium note that carries the `#federationBoard` label. The card's status is its board-view column, so dragging a card in the Trilium GUI changes its status and the stream reports `card.moved`. The board service is the only API writer. Trilium admits loopback callers only, so the board host must be the node that runs Trilium.
- **Identity.** A party bearer is the only identity the board trusts. The board stores its hash in a local table and never in the vault. The sender of a card comes from the bearer and never from the card. A node token or the legacy bearer never authenticates to the board.
- **Refusals.** Every refusal answers 404 with one body, so a caller cannot map the board. A claim on a card that another party holds answers 409.
- **Claims.** A claim re-reads the card under one service-wide lock and writes the claimant only if the card is still posted. Two simultaneous claims give one 200 and one 409. The guarantee holds only while one board process runs, so the host takes a lock file and a second process refuses to start.
- **Stream.** `GET /board/stream` is server-sent events with a heartbeat every 30 seconds. A client resumes with `Last-Event-ID`, and the board replays events up to 30 days old.
- **Door.** When `AWM_EDGE_BOARD` is set, the public edge mounts the board at `/board/`. The mount allows exact method-and-path shapes only. It answers before edge authentication. It strips every `X-Awm-*` header, `Cookie` and `X-Forwarded-*`, and it removes a node token or legacy bearer before it forwards the request.
- **Agents.** The `board` domain relays an agent's calls to the host with the swarm's token from the node's env file. An agent never holds the token. The card verbs refuse a caller that arrives from another node, because the token is this swarm's identity.

## The front door

The front door is how a swarm receives a request durably. The `door` service (`awm/services/representative/`) runs only when `AWM_FRONT_DOOR=1` on a fleet node. It subscribes to the board and claims request cards addressed to `node_swarm()`. It queues a message card without claiming it. It writes each card to a SQLite queue before it saves its stream cursor, and it catches up from `GET /board/cards` after a gap. It keeps two sessions alive through `cx start`: the representative and the secretary.

The representative reads the queue through `door`, chooses a domestic agent, hands it the card and records the hand-off. It never claims, completes or posts a card, and it never does a card's work. The agent that takes the card finishes it on the board. `skills/awm/board-card.md` fixes that convention.

- **Containment.** Cards come from other parties, so the representative reads them as data. It holds no file or shell tools. The mode gate (`ARCHITECTURE.md` § *Verb gating*) limits its awm verbs. Any session it starts is a delegate: no Bash, no `SendMessage` or `ListAgents`, file tools confined to the scope, and only the awm MCP server. Delegates work in scope `awm/door-work` unless the card names an existing scope.
- **Messaging.** Agents on one node message each other with Claude's `SendMessage`. Swarms message each other through the board. `scope post` stays passive mail that wakes no one.
- **One door per swarm.** This is a deployment rule. Set `AWM_FRONT_DOOR=1` on one node per `AWM_SWARM`. Nothing in code stops a second door.
- **Known limit.** The gate cannot cover built-in tools. The representative's `ListAgents` and `SendMessage` reach every session on the shared Claude daemon, so its instructions limit `SendMessage` to hand-offs.

## The legacy bearer over SSH

The shared bearer authenticated every peer before node tokens existed. It stays
until `AWM_PEER_LEGACY_BEARER=0`, and an outbound call offers it only to a
domestic peer. It carries no node identity, so the edge stamps it as the bare
`peer`. No join handshake exists, because an imposter could use one to solicit
everyone's credentials. Instead **SSH is the authentication channel**: host-key +
`authorized_keys` provide mutual auth, and a peer fetches the *current*
credential on demand:

```
ssh <peer> 'cat "$AWM_PEER_CRED"'
```

`$AWM_PEER_CRED` on each node points at the file the `auth` service keeps current
(`$AWM_DIR/services/auth/peer_cred.current`). The fetch is implemented in
`gatewayclient.fetch_peer_cred(ssh_alias)` (`ssh -o BatchMode=yes <alias> 'cat
"$AWM_PEER_CRED"'`), cached, and re-fetched on a 401 (credential rotated). It is
only ever called with a **peer** alias.

### Setup (per node) — the one trap that silently breaks this

`ssh host 'cmd'` runs a **non-interactive, non-login** shell, which does **not**
source the interactive part of `~/.bashrc`. So the export must be placed **above**
the interactive guard, or in `~/.ssh/environment`:

```bash
# ~/.bashrc — ABOVE the `case $- in *i*) ... esac` interactive guard:
export AWM_PEER_CRED="$HOME/agentic_workspace/.awm/services/auth/peer_cred.current"
```

(Path is host-specific — it is `$AWM_DIR/services/auth/peer_cred.current` for that
node's workspace. Confirm with `awm auth peer-credential`, which prints the path.)

Alternative: `~/.ssh/environment` with `PermitUserEnvironment yes` in `sshd_config`.

### ControlMaster

Mesh peers have no Duo, so a plain `ssh <alias>` is fast; if the host's ssh config
has `ControlMaster auto` + a `ControlPath`, the fetch transparently reuses the
existing master socket. No federation code manages the master — it is standard ssh
multiplexing, configured at standup.

## Peer directory — usage

```
awm peer add mira mira:12100 --ssh-alias mira    # record a peer (run on BOTH nodes)
awm peer set mira --relation foreign --swarm collins --principal john --public-key <key>
awm peer grant mira journals                     # a foreign peer may read journals
awm peer revoke mira journals
awm peer list
awm peer resolve mira
awm peer forget mira
```

`peer add` records the peer locally and `peer join` is the same verb. Run it on **both** nodes to make the link mutual (nothing is synced). `edge_url` may be bare `host:port` (coerced to `https://`). The name must equal the peer's `node_name()` (§ *Node identity and tokens*).

## Cross-peer calls

- **From an agent (MCP): the peer is an argument, never another tool.** The
  list holds the core domains, `providersOf` and the call-through tool `more`.
  Each domain tool takes an optional `peer` beside `verb`/`args`, and
  `more(domain, verb, args, peer)` reaches every other domain. Where a call lands
  with no `peer` is the domain's **default provider**: this node for an ordinary
  per-node domain, the owner for a declared singleton, the sole domestic peer for
  a domain no local service provides. `providersOf(tool=…)` (also
  `awm peer providers`) reports the valid providers and which is the default.
  A domain that only a domestic peer provides is always discoverable, so it shows
  in `more` and never as its own tool. A foreign peer is never a provider, so its
  domains reach no tool description. Merging each peer's whole catalog under
  `<domain>@<peer>` names is what this replaced: it multiplied the tool count by
  the peer count (29 domains → 82 tools on a two-peer node) while adding no
  capability. Those names no longer dispatch.
- **From a service (service→service):** `gatewayclient.call_peer(peer, service,
  fn, args)`, for example `ssh` calling `2fa` on mira. It resolves the peer via
  the local gateway and POSTs `{edge}/svc/{service}/fn/{fn}` over CA-verified TLS
  with a credential from § *Outbound credentials*. `RefCache` keys include the
  peer, so a local `2fa` and mira's never collide.
- **To another swarm's door:** `gatewayclient.invoke_peer(peer, name, args)`
  POSTs `{edge}/invoke`. That is the only path a foreign node exposes, so it is
  how `scope archive_search` reaches a peer.

### Outbound credentials

Every outbound call presents a node token first: `call_peer`, `invoke_peer`,
`subscribe_peer`, `fetch_peer_file` and the lease helpers all go through
`gatewayclient.peer_send`. The client gets the token from the local `auth`
service and caches it.

A domestic peer that answers 401 (a node that has not upgraded) gets one repeat
with the legacy bearer fetched over SSH. The client then calls that peer with
the legacy bearer for five minutes. A foreign peer never gets the legacy bearer.
Its refusal returns as sent. Only 401 triggers the fallback, because a 403 is an
authorisation verdict that another credential cannot change.

### Who provides what — and why the gateway still never relays

`peers.py` is the address book; `peer_catalog.py` is the capability half, holding
which domains each peer exposes. It is filled by a **background sweep** (one
supervised loop, ~2 min) rather than on demand, because a fetch costs an ssh plus
a TLS round trip to a host that may be asleep — the old inline-per-listing fetch
made every `list_tools` pay that. `GET /tools?view=domains&peers=1` therefore
never waits on a peer, cold cache included. The sweep asks each peer for its
**local** view (plain `?view=domains`); a peer's own union would carry *its*
peers, which this node may not have in its book and cannot dial. Peer chaining is
deliberately not a thing.

Dispatch resolves the target and, for a peer, raises a **redirect carrying that
peer's edge address** instead of forwarding: the caller dials the peer directly,
so the invariant that no peer bytes traverse a gateway holds exactly as before —
handing back an address is the same job `peer_resolve` already does. Only a caller
that declares it can follow one (`X-Awm-Peer-Redirect`, which just `awm-mcp` sets)
gets it; anyone else gets `421 Misdirected Request`. A request that cannot be
honoured **fails loudly** rather than running locally — a silent local fallback is
precisely the half-route this whole model exists to prevent.

**Which domains are singletons is declared, never inferred from env-var shape.**
Of the three selectors above only `AWM_TWOFA_PEER` means "the whole domain lives
elsewhere", and it seeds the table. `AWM_SOCIAL_PEER` does not — `social` runs
per-node and only individual *accounts* are singular, so re-homing the domain
would cost the node its own Slack, Gmail and buckets. `AWM_SSH_SLOT_PEER` does not
— only the arbiter *role* is fleet-global, not the `ssh` service every node needs
locally. Both keep a local default with the peer available as an override.
`AWM_DOMAIN_HOME_<domain>=<peer>` declares a future singleton without a code
change. A declared singleton has exactly **one** valid provider, not "local plus
the owner": two nodes each treating themselves as valid for one external resource
is the failure documented below — two listeners spending one Duo budget, two
logins on one Discord token.

## Cross-peer streaming

`gatewayclient.subscribe_peer(peer, service, topic)` is the streaming twin of
`call_peer` — the cross-peer analogue of `subscribe`. It opens
`wss://<edge>/svc/<service>/emit/<topic>` **directly on the peer edge** over
CA-verified TLS with a node token (§ *Outbound credentials*), and yields decoded frames **byte-for-byte
identically** to the local `subscribe`, so a consumer cannot tell a peer stream
from a local one. The peer's `httpsfront` edge authenticates the bearer during
the WS handshake (before `accept()`), so a refused credential surfaces as a
handshake rejection (`InvalidStatus` 401/403). `subscribe_peer` retries once
with a fresh credential and reconnects — the WS analogue of `call_peer`'s 401
retry. A foreign node cannot open this stream, because the edge answers 404 to
any WebSocket from a foreign node.

Auth is checked only at connect, so a mid-stream rotation (~12 h cadence) bites
only on the next reconnect. Consumers that need indefinite liveness wrap
`subscribe_peer` in their own reconnect loop (as the `/approve` listeners do) —
`subscribe_peer` itself keeps single-connection semantics plus the one
credential-refresh retry.

## Cross-peer bytes

The third transport. Neither of the first two can carry a file: `call_peer` is
JSON and fully buffered, `subscribe_peer` discards binary frames outright. So a
verb that produces a file returns the file's **address on the node that ran it**,
and the caller pulls the bytes down separately.

That address is the serving node's `fileviewer` static mount (`/files/<abs
path>`), which every node registers and `httpsfront` fronts as a catch-all — the
node token authenticates there exactly as it does on `/svc/*`, so no gateway or
edge change was needed here either. A foreign node cannot reach the mount.
`gatewayclient.fetch_peer_file(peer, url)` resolves the edge, presents the
credential as `call_peer` does, GETs over CA-verified TLS, and **streams** to a
local temp dir. The URL must
be origin-relative and redirects are not followed: the peer names the path, this
side names the host, so a reply can never aim a credentialed GET elsewhere.

Two things to know before returning a file from a service:

- **A path in a reply is a lie the moment the call is borrowed.** `social`'s
  `download_attachments` reported success and handed back a path on *mira*;
  callers on other nodes could never open it. Return `url` beside `path` (see
  `awm/services/social/awm/social/attachments.py`), and the MCP proxy
  (`gateway/peer_files.py`, shared by both proxy implementations) rewrites
  `path` to a local copy for any peer-routed reply — with a named `error` and
  `path: null` when it cannot, never a foreign path left in place.
- **The mount is denylist-masked and a masked file 404s exactly like a missing
  one.** `*.pem`, `*.key`, `*.token`, `credentials`, `secrets/**` and friends are
  unreachable by design; that is why the 404 message names both causes rather
  than reporting a bare status.

### Selecting local-or-peer — one branch, node-level config

A service that consumes a singleton (`ssh`→`2fa`, `ssh`/`2fa`/`auth`→`social`)
must route to the local service **or** a peer's edge from a single decision, so
it can never half-route. The selectors live in `gatewayclient`:

- `peer_env(var)` reads an env var; empty/unset → local.
- `call_maybe_peer(peer, service, fn, args)` — local `call` when `peer` is
  falsy, else `call_peer`.
- `subscribe_maybe_peer(peer, service, topic)` — local `subscribe` when falsy,
  else `subscribe_peer`.

The singleton's home is **node-level**: the node that OWNS the singleton leaves
the selector unset (all calls stay local); a node that BORROWS it exports the
selector to the owner's peer name. The env-var convention (`svc@peer` below is a label for a service on a peer, not a name any caller can dispatch):

- `AWM_TWOFA_PEER=<peer>` — `ssh` arms its Duo burst on `2fa@<peer>`.
- `AWM_SOCIAL_PEER=<peer>` — `ssh`/`2fa`/`auth` send Discord messages and
  subscribe to the `/approve` command stream on `social@<peer>`.
- `AWM_SSH_SLOT_PEER=<peer>` — `ssh` acquires a connection slot from the slot
  arbiter `ssh@<peer>` before a lockout-sensitive connect (see below).

So on **mira** (which owns both `2fa` and `social` and is the slot arbiter) these
are unset and everything is local; on borrowing nodes they are set to `mira`. This
is node-level env, deliberately not a per-host field: the same service code runs on
every node, and a hard-coded `2fa@mira` on the owning node would make it call
itself.

### Node identity — who a shared message came from

Several nodes write into **one** operator DM on Discord, so the same env file
carries the node's own name and address:

- `AWM_NODE_NAME=<name>` — the fleet name this node signs its password pushes and
  ssh lockout alerts with. Falls back to the OS hostname, which is the bug it
  exists for: mira's hostname is `pavilion`.
- `AWM_EDGE_URL=https://<addr>:<port>` — this node's edge *as another device
  reaches it*, used to build the autologin link.

`AWM_NODE_NAME` also names this node as the audience of every node token sent to
it (§ *Node identity and tokens*). Declare it on every node. The hostname
fallback is wrong for mira, and a peer book entry must match the declared name.

Read via `awm.config.node_name()` / `edge_url()`. Declare both. `edge_url()` can
fall back to the first enumerated address in `AWM_MESH_SUBNET` (default
`10.74.81.0/24`), but that is only right on a node that owns its own mesh
interface — capella's edge is reached at the *Windows* host's ZeroTier address,
invisible from inside WSL, the same asymmetry `AWM_TLS_EXTRA_SANS` exists for.
Undeclared and unenumerable yields `None`, and callers omit the link.

**The autologin link.** The password push carries `<edge>/__auth/link?p=<password>`
so a phone taps instead of transcribing. A live credential in a URL is acceptable
only because that route is never a page: it validates, 302s to a clean local path
with the cookie set, and sends `no-store` + `no-referrer`. Two things keep it that
way and must not be undone — the redirect-before-render shape, and uvicorn's
`log_level="warning"` in `proxy.serve`, which is what actually keeps query strings
out of the logs (`gateway/access_log.py` is dormant and protects nothing).

**Who a failure is attributed to.** The lockout alert names the *requester*, and on
the arbiter path that is not the node writing the message — `AWM_SSH_SLOT_PEER=mira`
means mira pages on capella's behalf. The requester's name therefore travels with
the lease (init frame, so a silent drop is still attributable; verdict frame for a
reported failure). An arbiter that interpolated its own name would reintroduce
exactly the defect above.

## Connection-slot arbiter — fleet-global single-attempt for ssh

The ssh circuit breaker enforces "exactly one attempt, then hold until an
operator clears it" so a lockout-sensitive host (fir: 10 failed Duo → Alliance
lockout) can't be marched to lockout by retries. That rule was **per node** (a
local lockfile + a per-host process singleton). Once several nodes drive the same
account through `2fa@mira`, the fleet's real attempt budget is N× what any one
node thinks — two nodes can each fire an attempt at fir. So the budget must be
**fleet-global**.

One node — `ssh@mira` — is the **slot arbiter**. For a host with a 2FA device, a
connect first acquires that host's slot from the arbiter; the arbiter grants at
most one in-flight attempt fleet-wide and, after a failure, refuses all attempts
until an operator clears it. Non-2FA hosts keep the per-node local-breaker path.

**The lease is a connection, not a timer.** The requester opens a **direct-session
WS** to `ssh@<arbiter>` and the OPEN socket *is* the lease (ZooKeeper-ephemeral /
etcd-keepalive style): held for exactly as long as the connection is alive, so a
live socket is proof of work in progress and a dead requester frees (or trips) its
slot the instant its socket drops. This reuses the gateway's existing
direct-session mechanism (which the `tts` and `stt` audio bridges also use) and
the edge's catch-all peer WS proxy — **no gateway or edge change**. The
client helpers `acquire_lease` / `acquire_lease_peer` / `acquire_lease_maybe_peer`
are the direct-session analogue of `call_peer` / `subscribe_peer`.

**Arbiter DFA (per host).** States `IDLE` / `LEASED` / `LOCKED`; `LOCKED` persists
to the arbiter node's lockfile (authoritative, central), `LEASED` is ephemeral
(dies with its socket). Because the reject-at-handshake path isn't available
(`session.opened` is sent before the handler runs), grant/deny rides as the
**first application frame** on the bridge (`{"lease":"granted"|"busy"|"locked"}`),
and drop = the bridge EOFs.

| state ↓ / input → | `open` | `verdict_ok` | `verdict_fail` | `drop` | `clear` (`/approve`) |
|---|---|---|---|---|---|
| IDLE   | → LEASED / grant | → IDLE | → IDLE | → IDLE | → IDLE |
| LEASED | → LEASED / busy | → IDLE | → **LOCKED** (+alert) | → **LOCKED** (+alert) | → IDLE |
| LOCKED | → LOCKED / deny | → LOCKED | → LOCKED | → LOCKED | → **IDLE** |

`LEASED --verdict_fail/drop--> LOCKED` is the whole point: a failed or dropped
attempt trips a global breaker. `LOCKED`'s only exit is an operator `/approve`
window (reusing the existing `_approve_until` one-shot). The arbiter is the **sole
notifier** on a LOCKED transition, so a requester's own attempt stays silent —
no double-paging. It fails **closed**: an unreachable arbiter refuses the connect
(no VPN/2FA/ssh, no MFA spent), which is the correct bias — you can't approve a
Duo whose approver you can't reach either.

**Client FSM (one attempt):** `open lease` → GRANT → hold while running
VPN+burst+ssh → master up → `verdict ok` → free; connect fail → `verdict fail`
(+reason) → LOCKED; REJECT / arbiter unreachable → refuse; **hold socket drops →
stop** (the socket is the single source of truth for both sides). Implemented in
`SSHService._slot_acquire` / `_slot_release` / `_lease_session` (arbiter) and
`_connect_through_arbiter` (requester); the ssh manifest declares
`sessions: [{"kind":"lease","transport":"direct"}]`.

**The verdict answers "was the MFA budget spent?", not "did the connect
succeed".** The slot exists solely to bound Duo attempts, so a connect that died
**before the auth phase** reports `verdict ok` — it spent nothing, the account is
not at risk, and holding the host would cost the operator an `/approve` for a
failure that was never theirs. `SSHService._is_preauth_failure` classifies the
captured ssh stderr and `_verdict_ok` maps it. The classification is deliberately
**asymmetric**: mistaking an auth failure for pre-auth means retrying and burning
the budget toward a real lockout, while the reverse is just a spurious hold — so
only unambiguous markers count (`kex_exchange_identification`, `Exceeded
MaxStartups`, `Connection refused`, DNS/routing), **any** auth-phase marker
(`Permission denied`, `Duo`, `MFA`, …) vetoes, an askpass deviation vetoes (the
Duo prompt was reached), and no/unrecognised evidence holds. It runs on the
**requester**, so a borrowing node gets it from its own tree; an arbiter node's
in-process connects need its tree updated too.

**Peer cred fetch (`gatewayclient.fetch_peer_cred`)** gates every peer call and
the lease, and the arbiter fails closed on it — so it retries transient ssh
failures (exit 255 / timeout; a plain `cat` spends no MFA, unlike the attempt it
guards) but raises at once on a definitive answer. `timeout` is a **total**
budget, not per-attempt, so retries can't multiply the worst-case fail-closed
latency. Async callers **must** use `fetch_peer_cred_async` — the sync fetch
shells out to ssh and will otherwise stall the whole service's event loop.

### Standing up a new node against a lockout-sensitive host

Install the target's **host keys from a node that already has them** before the
first connect — `ssh-keygen -F <host>` on capella or mira, appended to the new
node's `~/.ssh/known_hosts`. Otherwise `ssh` raises a confirm-the-fingerprint
prompt, the askpass refuses it (correctly — it is not a Duo menu), and the connect
fails. It costs no MFA attempt and no longer trips a hold, but it is a wasted round
trip, and taking the fingerprint from a node with a long history of using the host
beats accepting it blind on the new one.

## TLS

Peer edges present certs signed by the shared **remote-audio root CA** (the same
root `httpsfront` already uses). Cross-peer TLS is **CA-verified, never
`verify=False`** — a bearer over an unverified connection could be captured. The
CA path is `AWM_PEER_CA` / `REMOTE_AUDIO_CA_DIR` / `~/.config/remote-audio/ca/
ca.pem`; if it is absent the call raises loudly rather than falling back to
insecure.

**A new node gets `ca.pem` and deliberately not `ca-key.pem`:** it must verify its peers and must not be able to mint for the
fleet. Its leaf is cut on the CA holder with SANs covering the addresses it will be
dialed at, and both halves dropped into `awm/services/httpsfront/.certs/`.
`ensure_certs` recognises that state as a *trust consumer* and validates instead of
provisioning — fatal on a leaf that is missing, unchained, mismatched or expired;
a warning only when it fails to cover some newly-appeared docker bridge, because
taking the edge down for an address nobody dials is worse. It used to read a missing
key as a missing CA and re-mint, which replaced the fleet's root and surfaced as a
certificate error on every peer.

capella and mira both still hold a `ca-key.pem` — they predate this rule and each
minted their own leaf. That is not urgent (they hold the *same* root), but it means
either could re-mint the fleet root if its `ca.pem` were ever lost, so the goal is
one key-holder and the rest trust consumers.

`mic` used to keep its own copy of that code and is **enabled by default on every
node**, so it could win the boot race, put a key back, and swap the root before
httpsfront's guard ever ran. Its copy is gone: mic's page and audio moved onto the
hub behind httpsfront, so it mints nothing. `httpsfront` is now the only service
that touches the CA, and a test in its suite fails if a second copy reappears.

## Singletons vs per-node services

- **Singletons** (front a single external resource): `2fa` and `social`/the mira
  daemon are **canonical on mira** (always-on). Consumers reach them via the
  node-level env selectors above (`AWM_TWOFA_PEER` / `AWM_SOCIAL_PEER`), not a
  general auto-resolve — a borrowing node opts in explicitly.
- **Singleton *accounts*** are the same rule one level down, and the level that
  actually bites. `social` itself is per-node — every node wants its own Slack,
  Gmail and bucket access — but an individual identity inside it may still be one
  session fleet-wide. A Discord bot token is the case: a second login is not a
  second client, it is a collision, and the loser answers slash commands with
  `10062 Unknown interaction`. Mark such an account `singleton = true` in
  `social.toml`; the node with `AWM_SOCIAL_PEER` unset owns it, and a borrowing
  node connects it not at all and forwards verbs naming it to the owner. So the
  service runs everywhere, the identity exists once, and callers on either node
  are oblivious. Whether a shared account tolerates concurrent logins is the
  platform's answer, not ours — Discord does not, which is why the flag is
  per-account and opt-in rather than inferred.
- **A node with no local install of a per-node service borrows the whole
  domain.** That is a fact about the node, not about the service. altair runs no
  `social` — no `social.toml`, no accounts — so with two peers advertising it the
  catalog resolved `social` as `ambiguous` and every call had to name a peer by
  hand. `AWM_DOMAIN_HOME_social=mira` in altair's `.awm/env` makes it a declared
  singleton *there*: one provider, one default, no `peer` argument. Node-local
  and deliberately not fleet state — capella, which does run `social`, must never
  get it, or it loses its own accounts. Note that this and `AWM_SOCIAL_PEER` are
  independent: the latter tells *services* on this node where to send Discord
  traffic, the former tells the *catalog* where an agent's call lands.
- **Everything else is per-node** (local resource or node-owned state): visible
  on both, calls default local, nothing synced.

## Deploy

Land on `dev` and promote `feat → dev → release`. Nodes track `release`, so a fix
hand-patched onto a running node is a fix that gets reverted by the next update.

### Updating a node

A node's `~/agentic_workspace` is a git checkout of `release`, with an `awm` remote
pointing at capella's bare repo over ssh. To update it:

    git fetch --no-tags awm release && git reset --hard awm/release
    git clean -fd            # NOT -fdx — .awm/ is ignored and must survive
    awm deploy

Three things about that sequence are load-bearing. **Fetch to the tracking ref,
not the branch**: `fetch awm release:release` is refused once `release` is
checked out, and `reset --hard` is what moves a checked-out branch anyway.
**`clean` without `-x`**: the reset writes every path the commit contains but
removes nothing it omits, so files release deleted linger — including `.py`
leftovers inside editable-installed packages, which stay importable. `-x` would
take `.awm/` with them, and that holds the Duo credentials, `social.toml` and
every service DB. **`awm deploy`, not a bare unit restart**: it reinstalls dists
if the set changed, rebuilds changed pages, restarts, reaps orphans, and then
verifies every enabled service and built page came back — a restart alone silently
skips all of that, and a service whose dist never got installed comes back as a
crash loop rather than an error. It probes which `awm.service` supervises the
gateway rather than assuming; the fleet is mixed (capella a system unit behind
sudo, mira and altair per-user), and `sudo systemctl` on a per-user host addresses
a different systemd instance, reports the unit missing, and leaves the old gateway
running.

`awm deploy` needs `mamba` and the env's `node` on `PATH` and a populated
`awm/node_modules`. Neither is implied by a fresh clone: `environment.yml` carries
no nodejs, so a node that has never built pages needs `mamba install -n awm nodejs`
and one `npm install` in `awm/` before its first deploy.

`enabled.json` must stay explicit for the same reason: a service absent from it is
*enabled*, so anything added to the tree since the last sync starts on the next
boot, on the node least able to absorb a crash loop.

mira is supervised by a per-user systemd unit; capella's prod is
started from its PID file and orphaned to init. `awm gateway restart` probes which,
via `systemctl --user is-active` — deliberately not `is-enabled`, since a dangling
unit symlink reports enabled-but-bad and cannot be started.

## Not done

**Cross-node data sync.** Each node owns its own `notes` / `writing` / `drawio` /
`precedence` / `scopes` databases and nothing reconciles them — replication is
deliberately out of scope (see the top of this file). So "my notes" means "this
node's notes"; reach across explicitly with the `peer` argument when you need to. Worth
designing before a second node becomes a real writing surface.
