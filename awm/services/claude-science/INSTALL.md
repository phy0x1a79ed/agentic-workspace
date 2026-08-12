# claude-science

Claude Science — Anthropic's local science workbench — supervised by awm, put
on the mesh behind awm's edge session, and wired to a curated slice of awm's own
tool surface.

Upstream is a single self-contained binary that runs a daemon, serves a web UI
on loopback, and runs Claude's Python/R/shell in a bubblewrap sandbox. It is a
desktop application that happens to run on a server. Everything here is about
making that fact survive contact with a fleet.

## What this replaces

Three hand-written systemd units and a 300-line fork of `awm/services/httpsfront`
living in `$HOME`, with an `ExecStartPre` that copied awm's TLS material to a
second location. The fork had no auth gate, so its zero-click `/_signin` route
handed workbench ownership to anything that could route to the port. Moving to
the real front is a security fix first and tidiness second.

## The contract

**The daemon is adopted, not owned.** Every other awm service parents its child
and lets the gateway's supervision cover the whole stack. This one deliberately
does not: the workbench holds live conversations, sandbox binds, and analysis
runs, and `awm gateway restart` happens on every deploy. So this service starts
a daemon if none is running, adopts one that is, restarts one that has died —
and leaves it running when the service stops. `stop` is an explicit verb.

The practical guarantee: **restarting the gateway does not change the daemon's
pid.** If it does, something is wrong.

**Detaching is not what buys that — the cgroup is.** `serve --detached` already
gives the daemon its own session and `PPID 1`, which defeats every signal-based
teardown. It does nothing about `systemctl restart awm`, which kills by
*control group*, and a cgroup is inherited by every descendant however it
detaches. So a daemon this service spawned died on exactly the deploy action it
was supposed to survive, while one adopted from its own unit sailed through —
the bug was invisible until this service became the thing doing the spawning.
The launch therefore goes through `systemd-run --user` into a transient
`claude-science-daemon.service` under the *user* manager, a cgroup `awm.service`
does not own. `status` reports `daemon.user_unit`; a null there means this node
has no user manager (a container, a bare dev box) and the daemon will not
survive an awm restart. The unit is transient, so a reboot leaves nothing
behind and this service starts a fresh daemon as it would on any cold node.

**Two ports, two origins.** Upstream serves generated-HTML previews from a
second port so a page Claude wrote cannot read the session that wrote it. That
boundary is a browser origin, so it survives only if we keep two of them. Hence
two fronts rather than one, and no path-prefix mount.

**Why not a gateway `kind=url` mount.** The binary has `--base-path` and the
gateway proxies url mounts, so this looks like a one-liner. It is not: the
gateway's url proxy strips `Cookie` on the second hop, forwards *no* headers at
all on WebSocket upgrades (no `Cookie`, no `Origin`, no subprotocol), and
comma-collapses duplicate `Set-Cookie`. The workbench authenticates with
`operon_auth`/`operon_csrf` cookies and origin-checks its upgrades. It would
fail in three places. Don't re-derive this.

**Why not Docker.** The ~6 GB conda tree, the OAuth tokens and the conversation
database all live in the data directory, which has to be a persistent volume
either way — a container relocates that cost rather than removing it. Baking it
into an image breaks the update path: the binary self-updates with sha256
verification and rolls back by version, whereas an in-container update writes to
a layer that vanishes on recreate. And the sandbox is bubblewrap: nested
unprivileged user namespaces inside Docker on WSL2 are fragile, and upstream's
own guidance (`--dangerously-no-sandbox` — "only use in already-isolated
environments (CI, containers)") points at *disabling* the inner sandbox, which
is a downgrade for a tool deliberately pointed at host data. A container would
win for many disposable instances, or a node where `$HOME` is off limits.
Neither applies here.

## Registrations

Four, from one process:

| kind | name | prefix / port | what |
|---|---|---|---|
| `service` | `claude-science` | `/svc/claude-science` | the verbs, plus supervision |
| — | (TLS front) | `0.0.0.0:12201` | the workbench UI, behind `awm_session` |
| — | (TLS front) | `0.0.0.0:12202` | generated-HTML previews, separate origin |
| `url` | `claude-science-mcp` | `/claude-science/mcp` | the MCP bridge's loopback listener |

The two fronts are not gateway registrations — they are listeners this process
owns, the same shape as `httpsfront`'s own. They die with the service; the
daemon does not.

A fifth thing appears without this process doing anything: the control panel at
`/ui/claude-science`, which the gateway mounts on any node where the page has
been *built*. Page discovery keys on a built `dist/` and has no profile gate, so
a node that deploys the tree without running the service still mounts the panel
— and it will be dead, because the page calls the local `/svc/claude-science`
with no peer selector.

**The verb surface is `science`, not `claude-science`.** The gateway folds the
MCP surface by splitting a projected tool name on its *first* underscore, so
`claude_science_status` would land as domain `claude`, verb `science_status` —
the service's name cut in half. The manifest therefore projects `science_*`:
`awm science status`, `mcp__awm__science {verb:"status"}`. The service, the
prefix and the folder keep the full name.

## Install

```
./install.sh
```

Installs the Python bits (including `httpsfront`, whose cert minting, auth gate
and reverse proxy the fronts are a *configuration* of), writes `.runtime-env`,
and installs the workbench binary via upstream's own installer if it is absent.
It does **not** build the binary and does **not** provision the data directory:
the binary's own `update --to <version>` already downloads, sha256-verifies and
atomically swaps, and the ~6 GB of Python/R environments are provisioned by the
daemon on first run, in the background.

Prerequisites the installer checks for, because the sandbox is not optional —
the daemon refuses to start rather than run code unsandboxed:

```
sudo apt-get install -y bubblewrap socat      # bwrap >= 0.8.0
```

**On Ubuntu 24.04 the package alone is not enough.**
`kernel.apparmor_restrict_unprivileged_userns=1` transitions any unconfined
process that creates a user namespace into the `unprivileged_userns` profile,
which carries `audit deny capability` — so bwrap cannot write its uid map and
every sandbox dies with `setting up uid map: Permission denied`. Grant that one
binary the permission rather than relaxing the sysctl host-wide: an
`/etc/apparmor.d/bwrap` shim of the shape Ubuntu ships for Electron apps
(`/etc/apparmor.d/obsidian` is the model — `flags=(unconfined)` plus a bare
`userns,`), then `apparmor_parser -r`. Prove it before deploying —
`bwrap --ro-bind / / --dev /dev --unshare-all /bin/true` must exit 0 — because
otherwise it surfaces much later as a daemon that will not start.

Then opt this node in, via the profile gate in `service.toml`. Deployments are
**per-node and independent**, so every node that wants a workbench does this for
itself:

```
# in <workspace>/.awm/env
AWM_PROFILES=claude-science
# every browser-reachable origin besides the one derived from AWM_EDGE_URL —
# capella: https://172.25.181.70:12201 (the WSL interface)
# altair:  https://192.168.100.142:12201,https://127.0.0.1:12201
CLAUDE_SCIENCE_EXTRA_ORIGINS=...
```

**On a trust-consumer node, provision the fronts' leaf by hand.** The fronts
mint their own certificate under `<service>/.certs/`, which works only where the
CA *key* is (capella). Anywhere else they come up `serving: false` with a
`TrustConsumerError` naming the exact SAN set they need — a node holding
`ca.pem` without `ca-key.pem` must not mint, because minting would replace the
fleet's trust root and surface as a certificate error on every peer. The node's
`httpsfront` already holds a leaf for the same host with the same
auto-enumerated SANs, so copy that pair across rather than minting a second one
elsewhere and moving a private key over the network:

```
cp -p awm/services/httpsfront/.certs/{cert,key,ca}.pem \
      awm/services/claude-science/.certs/
```

Both are gitignored host state, so a deploy's `git clean -fd` leaves them alone.
Nothing re-cuts a consumer's leaf when it expires — check `fronts[*].san` and
the expiry if the fronts ever stop serving.

Then deploy. **Export the profile into the deploy shell as well** —
`AWM_PROFILES=claude-science awm deploy`. The CLI does not read `.awm/env`
(only the gateway does), so deploy's verification set is computed without the
profile and silently omits this service: it reports success without ever having
checked the workbench came back.

## Environment

| Var | Default | Effect |
|---|---|---|
| `CLAUDE_SCIENCE_BIN` | `~/.local/bin/claude-science` | the binary to supervise |
| `CLAUDE_SCIENCE_DATA_DIR` | `~/.claude-science` | the workbench's data dir |
| `CLAUDE_SCIENCE_UPSTREAM_PORT` | `12203` | loopback UI port |
| `CLAUDE_SCIENCE_SANDBOX_PORT` | `12204` | loopback preview port |
| `CLAUDE_SCIENCE_FRONT_PORT` | `12201` | mesh TLS port, UI |
| `CLAUDE_SCIENCE_SANDBOX_FRONT_PORT` | `12202` | mesh TLS port, previews |
| `CLAUDE_SCIENCE_PUBLIC_HOST` | from `AWM_EDGE_URL` | host a browser reaches this node at |
| `CLAUDE_SCIENCE_EXTRA_ORIGINS` | — | comma-separated extra allowed origins |
| `CLAUDE_SCIENCE_MCP_ALLOW` | built-in list | JSON `{domain: [verb, …]}` for the bridge |
| `CLAUDE_SCIENCE_HEALTH_INTERVAL_S` | `20` | supervision loop period |

**The ports are free choices in the code, but not on every node.** capella runs
awm inside WSL, where only specific ports are forwarded by the Windows-side
portproxy — 12100 (the gateway front), 12201 and 12202. Moving a front to an
unforwarded port takes the workbench off the mesh there, and adding one needs an
elevated run on the Windows side. A native-Linux node has no such constraint;
the defaults are then just defaults, and two nodes both on `:12201` are
distinguished by address alone.

**The public host is declared, not enumerated,** for the same reason
`AWM_EDGE_URL` and `.sans` are: on a WSL node the mesh address belongs to the
Windows host and is invisible from inside. It defaults to the host in
`AWM_EDGE_URL`, which is right on every node that declares one.
`--allow-origin` is matched exactly on scheme+host+port and gates WebSocket
upgrades, so a missing origin shows up as a UI that loads and then silently has
no socket. The list is built **once, at daemon launch**, and this service adopts
a running daemon rather than relaunching it — so a newly-added origin needs an
explicit `awm science restart`, not a service or gateway restart.

## The MCP connector — not possible on 0.1.27/Linux

**There is no way to give this workbench awm's tools.** Both connector kinds are
closed, for different reasons, and neither has a setting that opens it. This is
measured on 0.1.27, not inferred; re-measure it on a new build before believing
it still holds.

**Remote is closed by address.** The daemon's URL guard (`safeFetch`) requires
https *and* a public host, rejecting 10/8, 192.168/16, 172.16/12, 100.64/10,
loopback, link-local and any name ending `.local` / `.internal` / `.lan` /
`.home.arpa`. Every address this fleet has is on that list, so a remote
connector cannot point at any node's gateway — TLS on the edge does not help,
and there is no override or allowlist.

**Local is closed by sandbox.** A local (stdio) server does run on the host
rather than in the analysis sandbox, but it gets a bwrap sandbox of its own with
`--unshare-net`: an empty network namespace, no route anywhere, and outbound
only through a proxy whose `NO_PROXY` covers every private range. From inside,
`127.0.0.1:7819` is `Network is unreachable`. AF_UNIX is blocked by seccomp
(`runtime/*/seccomp/*/unix-block.bpf`), so a unix-socket relay fails `connect()`
with `EPERM` even when the socket is plainly visible — and `[sandbox.network]
allow_unix_sockets` is macOS-only config that changes nothing here.

Two facts are worth keeping anyway, because both cost a diagnosis to learn:

- Registering a local server and *connecting* it are separate events. A server
  whose command cannot run registers with a clean 200 and reports its failure
  only in `/api/mcp-servers/:id/tool-permissions`, as an `error` string beside
  an empty tool list. `science connector` probes and reports it; a status check
  alone would call it healthy.
- Most of `$HOME` is invisible inside the sandbox, so a server installed there
  fails to exec with a bare `not found`. `[sandbox] user_read_paths` in
  `<data-dir>/config.toml` is the fix, and altair keeps grants for the awm
  interpreter and source tree so this is one less thing to rediscover.

The one channel that does cross is the filesystem: a FIFO pair in a server's own
`workspaces/_mcp-<name>/` directory round-trips fine, which is the same shape
upstream uses for its own proxy (a socat under `sbx-bind-src/` publishing a unix
socket as loopback TCP inside the namespace). Tunnelling awm through one would
work and would deliberately defeat a boundary upstream built on purpose — a
decision, not a workaround, and one a future build can break silently.

**The HTTP bridge stays** at `/claude-science/mcp`, and its allowlist still
governs it. Nothing in this fleet can consume it as a *connector*, but it is a
working MCP-over-HTTP endpoint for awm — usable by any MCP client that is not
this workbench.

**The allowlist is a security control.** awm has no per-caller mode and no
read-only credential — anything on the loopback bus can call `ssh`, `compute`,
`social`, gateway control and the agent write verbs, and the `agent` domain's
own verb gate documents itself as *not* a trust boundary. So the bridge ships an
explicit `{domain: verbs}` list, enforced on `tools/call` and not merely on
`tools/list` (hiding a tool is not refusing it). It defaults to read/query verbs
of `scope`, `project`, `notes`, `drawio`, `graphify`, `dvc`, `artifact`,
`precedence`. Widen it in `CLAUDE_SCIENCE_MCP_ALLOW`, deliberately.

## Host file access

Claude sees only what it is granted. Grants are consent rows plus live bind
mounts, persisted and replayed at boot, and each carries a mode — `ro` or `rw`.
Grant the workspace read-only with `awm science grants --path <p> --mode ro`;
called with no path the verb lists what a node already has.

| Path | Mode |
|---|---|
| `<workspace>/projects` | `ro` |
| `<workspace>/data` | `ro` |
| the workbench's own working directory | `rw` (outputs) |

Three things that bite:

- **Symlinks do not work.** A grant root must reach the path symlink-free, so a
  curated directory of symlinks into the workspace is refused. Grant real
  directories — which also means `.awm/data` (a symlink) is not grantable; grant
  the real `data/` instead.
- **Order matters.** Granting `ro` under an existing `rw` grant is refused
  rather than silently downgrading the shared bind. Flip or remove the broader
  grant first.
- **This was thought to be browser-only, and is not.** The daemon exposes the
  same loopback API its own UI calls, and this service can already mint the
  owner credential for it — `awm.claude_science.api` does the nonce exchange and
  then authenticates as a **bearer**, which matters: the token works as a cookie
  too, but that path additionally demands an `Origin` the daemon recognises and
  a matching `x-operon-csrf` header, while bearer auth skips the CSRF hook
  entirely. Grants are per-node host state, so they do not travel with a deploy;
  that is why they are a verb rather than a config file.

## Verify

```bash
# the service, from this node
awm services list | grep claude-science          # enabled, running, ready
awm science status                        # daemon + install + fronts + bridge
awm science grants                        # host file access, as the daemon has it
awm science connector                     # local MCP servers (none is expected)

# the fronts, from anywhere on the mesh
curl -sk -o /dev/null -w '%{http_code}\n' https://<edge-host>:12201/   # 401/login
curl -sk -o /dev/null -w '%{http_code}\n' https://<edge-host>:12202/   # 401/login

# the MCP bridge
curl -s http://127.0.0.1:7819/claude-science/mcp                       # bridge status
curl -s -X POST http://127.0.0.1:7819/claude-science/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 400
curl -s -X POST http://127.0.0.1:7819/claude-science/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"awm__ssh__connect","arguments":{}}}'           # refused

# adoption: the deploy-safety guarantee
awm science status | grep pid ; awm gateway restart
awm science status | grep pid                                   # same pid

# from a node that runs its own — resolves local, never a peer's
awm peer providers science          # default: local
```

In a browser on the mesh: open `https://<edge-host>:12201/`, log in with the awm
session, land in the workbench, and reload — the session persisting is what
proves `X-Forwarded-Proto` and `X-Forwarded-Host` reached the daemon. Start a
conversation (proves the WebSocket upgrade passed the origin check) and have
Claude render an HTML preview (proves the `:12202` front).

## Scope and caveats

- **One instance per node, and they share nothing.** Each deployment has its own
  binary, data dir, conversations and grants; there is no sync and no notion of
  a primary. The profile gate is the whole opt-in. Conversations move between
  nodes only by `claude-science import <data-dir|.db>`, which merges one way and
  has no dry run and no undo — stop the daemon on *both* ends first (the source
  so its WAL is checkpointed, the target because the merge writes to a database
  it holds open) and copy the org directory aside before starting.
- **Two providers cost a third node its default.** `science` resolves to *local*
  on any node that runs it, but a node running it nowhere and booking two
  providers gets no default at all and must pass `peer=` — the catalog refuses
  to guess. So standing up a second instance is what breaks bare `awm science
  status` on the nodes that have none. Give a node its own instance, or name the
  peer.
- **A stopped daemon is respawned within `HEALTH_INTERVAL_S`.** `science stop`
  means it for about twenty seconds; the supervision loop has no notion of a
  deliberate stop. To hold it down — for a data-dir merge, say — stop the
  *service* first (`awm services stop claude-science`), which leaves the adopted
  daemon running, then stop the daemon.
- **The binary self-updates by default.** `status` reports the running version;
  `--no-auto-update` is a knob, not our default, and `update --to <version>`
  both pins and rolls back.
- **The fronts do not survive this process.** That is intended — they are
  plumbing. If they are down and the daemon is up, the workbench is still
  reachable on loopback and `status` will say which half is broken.
