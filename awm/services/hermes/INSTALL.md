# hermes

Nous Research's [Hermes Agent](https://github.com/NousResearch/hermes-agent) —
a self-improving CLI agent with a web dashboard — supervised by awm, with its
GUI on a TLS front behind awm's edge session and its inference pointed at the
same OpenRouter account and model opencode uses on this node.

Upstream is a git checkout with its own Python venv, its own managed Node, and a
Vite/React dashboard it builds itself. It is a personal-workstation tool that
happens to run on a server. Everything here is about making that fact survive
contact with a fleet.

## The contract

**The dashboard is adopted, not owned.** Every other awm service parents its
child and lets the gateway's supervision cover the whole stack. This one
deliberately does not: the dashboard holds live chat sessions and PTYs, and
`awm gateway restart` happens on every deploy. So this service starts a
dashboard if none is listening, adopts one that is, relaunches one that has
died — and leaves it running when the service stops. `stop` is an explicit verb.

The practical guarantee: **restarting the gateway does not change the
dashboard's pid.** If it does, something is wrong.

**Detaching is not what buys that — the cgroup is.** `systemctl restart awm`
kills by *control group*, and a cgroup is inherited by every descendant however
it forks or `setsid`s. The launch therefore goes through `systemd-run --user`
into a transient `hermes-dashboard.service` under the *user* manager, a cgroup
`awm.service` does not own. `status` reports `dashboard.user_unit`; a null
`unit` there means this node has no user manager (a container, a bare dev box)
and the dashboard will not survive an awm restart. The unit is transient, so a
reboot leaves nothing behind and this service starts a fresh dashboard as it
would on any cold node.

**Liveness is the listening socket, not an HTTP reply.** A dashboard mid
inference can be slow to answer, and respawning on that would interrupt the
session this arrangement exists to protect. Nothing accepting a TCP connection
on the port is unambiguous; a slow reply is not. `status` still reports the HTTP
probe — it is richer — but nothing is ever killed on it.

**The dashboard owns an origin; it cannot live under a path prefix.** This is
the one design constraint the whole shape follows from, and it looks wrong on
first inspection, so it is worth being precise about.

Upstream *appears* to support subpath mounting. Give it `X-Forwarded-Prefix` and
it rewrites `index.html`'s asset URLs, rewrites `url()` references inside its
stylesheets, and publishes `window.__HERMES_BASE_PATH__` for the SPA to prepend
to `/api/…`. Mount it under a prefix and the shell paints, the sidebar fills in,
the first route renders. It still does not work: the SPA's *lazy* route chunks
are fetched by a loader whose asset base was frozen at `/` when the bundle was
built, so under any prefix each one is requested from the server root. Script
preloads fail quietly there, but a stylesheet preload is awaited — so the first
route carrying CSS, which is chat, throws and paints nothing at all.

**A build-time constant is not a header-addressable property.** No gateway flag
reaches it and no proxy can rewrite around it, so there are exactly two options:
serve the upstream at a root of its own, or rebuild its bundle. Rebuilding puts
the fix inside a checkout `hermes update` rewrites in place — with
`updates.non_interactive_local_changes: stash`, which would silently drop it —
so it is the front. `dsh` reached the same conclusion about the same class of
upstream; `claude-science` reached it for different reasons (the url proxy
strips `Cookie`, forwards no headers on a WebSocket upgrade, and comma-collapses
`Set-Cookie`). Three services, three routes to one answer. Don't re-derive it.

**The Origin rewrite is load-bearing.** The dashboard re-runs its DNS-rebinding
guard on every WebSocket upgrade, checking `Host` *and* `Origin` against the
interface it bound. `httpsfront` drops the inbound `Host` so httpx derives a
loopback one, which satisfies the first half, but forwards `Origin` verbatim —
leaving the guard holding a mesh origin against a loopback bind, and refusing
the upgrade. `front.py` passes `rewrite_origin=True` for exactly this. Without
it the GUI loads perfectly and silently never streams, because every part of
this dashboard that matters is a WebSocket.

**The dashboard binds loopback and therefore runs with its auth gate off.**
Everything reaching it comes through the front, so the awm edge session is the
real boundary — the same posture as every other awm page. Re-check that the day
the dashboard is bound non-loopback: at that point it fails closed until a
password or OAuth provider is configured, and starts using cookies.

**The leaf is borrowed, never minted.** This node holds `ca.pem` without
`ca-key.pem` — a deliberate trust *consumer* — so `ensure_certs` validates a
pre-placed leaf rather than minting a fleet-incompatible root. `front.py` copies
`httpsfront`'s pair in: the leaf is port-independent and its SAN set already
covers this host's mesh address, so a second front on a second port needs the
same pair, not a new one. It looks in the sibling `httpsfront` of its own tree
first, then in the **canonical** workspace — canonical rather than local because
a shadow overlay runs against an isolated `.awm-shadow` root that holds no certs
at all, and reading it leaves the front looping on `TrustConsumerError`.

## What serves what

Three things, and only the first two come from this process:

| where | what | who runs it |
|---|---|---|
| `/svc/hermes` on the gateway | the verbs, plus supervision | this service |
| `https://<mesh>:12401/` | the dashboard itself, at its own root | this service's front thread |
| `/ui/hermes` on the shared edge | the landing page — health, and a link out | the gateway, from `awm/pages/hermes/dist` |

The front dies with this service; the dashboard does not. The landing page is an
ordinary awm page discovered on disk, so nothing here registers it — and being
served from the shared edge rather than from the front is what lets it still
answer when the front is the part that is broken. It reports the dashboard
process, the front and the model route separately, because those fail
independently and need different fixes.

`hermes` is already a single token, so the gateway's MCP fold — which splits a
projected tool name on its *first* underscore — leaves the surface intact:
`awm hermes status`, `mcp__awm__hermes {verb:"status"}`. Keep the `hermes_`
prefix on every manifest `tool` name and it stays that way.

## Install

Two halves. The Python service:

```
./install.sh
```

It installs the adapter and its component libraries, writes `.runtime-env`, and
reports whether the Hermes runtime is present. It deliberately does **not**
install that runtime: it is ~2 GB and takes several minutes, and `hermes update`
owns it in place afterwards.

### The runtime

```
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
     -o /tmp/hermes-install.sh
bash /tmp/hermes-install.sh --skip-browser --skip-computer-use \
     --skip-setup --non-interactive
```

`--skip-browser` and `--skip-computer-use` drop the Playwright/Chromium and
cua-driver stages: awm already has `rlm-browser` and its own compute surface, so
they are dead weight here. `--skip-setup --non-interactive` keeps the wizard
away — the configuration below is explicit. The installer needs `git`, `curl`
and `xz-utils`, and installs `ffmpeg` and a managed Node 26 under
`$HERMES_HOME/node` (it refuses to trust an ambient one).

Then configure it. This mirrors `~/.config/opencode/opencode.jsonc` point for
point, which is the whole intent — one provider, one key, one MCP catalog, one
shared instruction file across the harnesses on this node:

```
hermes config set model.provider openrouter
hermes config set model.default deepseek/deepseek-v4-flash-0731
hermes config set model.base_url https://openrouter.ai/api/v1

# OPENROUTER_API_KEY in ~/.hermes/.env — the same key opencode holds in
# ~/.local/share/opencode/auth.json. Copy it programmatically; never paste it
# into a terminal you do not own the scrollback of.

hermes mcp add awm --command /home/<you>/lib/miniforge3/envs/awm/bin/awm-mcp \
  --env AWM_WORKSPACE=/home/<you>/agentic_workspace \
        AWM_EXPOSED_HOST=127.0.0.1 AWM_EXPOSED_PORT=12100

ln -sfn ~/.claude/CLAUDE.md ~/.hermes/SOUL.md
```

**`SOUL.md` replaces the agent's default identity — it does not extend it.**
Symlinking it to the shared instruction file is the analogue of opencode's
`~/.config/opencode/AGENTS.md` symlink and gets the workspace's operating rules
into every session; the cost is Hermes' own two-sentence identity paragraph,
preserved beside it as `SOUL.md.hermes-default`. Project context is separate and
needs nothing: Hermes walks `AGENTS.md` from the git root down and prefers
`AGENTS.override.md`, which is how a session under this workspace picks up
`WORKSPACE.md`.

Verify with `hermes doctor` — it checks the key, the config version, and live
OpenRouter connectivity. Two npm advisories against upstream's vendored web
workspaces are expected and are build-time tooling.

### Opt this node in

Deployments are **per-node and independent**, so every node that wants a Hermes
does this for itself:

```
# in <workspace>/.awm/env
AWM_PROFILES=hermes
```

Then deploy. **Export the profile into the deploy shell as well** —
`AWM_PROFILES=hermes awm deploy`. The CLI does not read `.awm/env` (only the
gateway does), so deploy's verification set is computed without the profile and
silently omits this service: it reports success without ever having checked the
dashboard came back.

## Environment

| Var | Default | Effect |
|---|---|---|
| `HERMES_BIN` | `~/.local/bin/hermes` | the launcher to drive |
| `HERMES_HOME` | `~/.hermes` | config, sessions, skills, logs, the checkout |
| `HERMES_DASHBOARD_PORT` | `9119` | loopback dashboard port |
| `HERMES_FRONT_PORT` | `12401` | mesh-facing TLS port for the GUI |
| `HERMES_TRANSIENT_UNIT` | `hermes-dashboard` | transient user unit name |
| `HERMES_HEALTH_INTERVAL_S` | `20` | supervision loop period |
| `HERMES_START_TIMEOUT_S` | `180` | how long to wait for the bind |

`HERMES_HOME` is passed explicitly on every invocation rather than inherited: a
service that silently managed a different profile than the one it reports on
would be worse than no service at all.

## Verify

```
awm hermes status                       # listening, pid, unit, front, model
awm hermes url                           # the front, the landing page, loopback
curl -s -o /dev/null -w '%{http_code}\n' $AWM_EDGE_URL/ui/hermes/   # 200 with a session

# the guarantee
awm hermes status | jq -r .dashboard.pid
awm gateway restart && sleep 20
awm hermes status | jq -r .dashboard.pid   # same pid
```

**A 200 is not evidence that the GUI works.** The failure this arrangement
exists to prevent is a route that loads its own chunks and throws — the shell
still paints and every curl still passes. Open the dashboard in a real browser,
go to **Chat**, and check that the network log has no 404 and that `/api/ws`,
`/api/events` and `/api/pty` all reach 101. Two of those three prove the parts
a curl cannot reach.

## Not done here

**Hermes is not a spawnable harness.** `agent_spawn agent_cli="hermes"` would
need an `agentcore/hermes_backend.py` beside `opencode_backend.py`. Config
parity is what "mirrors opencode" meant; the spawner is a separate piece of work
and easy to add later.

**Nothing pins a version.** `hermes update` rewrites the checkout at
`$HERMES_HOME/hermes-agent` in place, and this service supervises whatever that
produces. An update needs an `awm hermes restart` to reach the running
dashboard.
