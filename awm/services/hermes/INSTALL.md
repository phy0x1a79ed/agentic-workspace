# hermes

Nous Research's [Hermes Agent](https://github.com/NousResearch/hermes-agent) —
a self-improving CLI agent with a web dashboard — supervised by awm, with its
GUI mounted on the gateway behind awm's edge session and its inference pointed
at the same OpenRouter account and model opencode uses on this node.

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

**Why a gateway `kind=url` mount here, when `claude-science` refuses one.** That
service's INSTALL.md argues url mounts are unusable, and it is right about its
own upstream. The three failures it names are the url proxy stripping `Cookie`,
forwarding *no* headers on a WebSocket upgrade, and comma-collapsing duplicate
`Set-Cookie`. None of them bind a loopback-bound Hermes dashboard:

- on a loopback bind its auth gate is off, and the SPA authenticates with a
  session token injected into `index.html` — no cookie is involved;
- its REST calls carry that token in `X-Hermes-Session-Token`, which the proxy
  passes through; the stripped `Authorization` is unused;
- its WebSockets carry it as a `?token=` query parameter, which survives a
  header-less upgrade.

What *was* missing is the path. The proxy forwards `request.url.path` verbatim
and the dashboard serves at the root, so `/ui/hermes/api/status` reached it as
`/ui/hermes/api/status` and 404'd. Hence the registration's `strip_prefix` (see
`awm/AGENTS.md` § *External registrations*), which peels the mount prefix off
and sends it back as `X-Forwarded-Prefix`. The dashboard reads exactly that
header to set `window.__HERMES_BASE_PATH__`, which the SPA prepends to every
`/api/…` URL it builds. Verified end to end: page load, REST through the prefix,
the event WebSocket, and the chat tab's PTY echoing keystrokes.

**Re-check all of that the day the dashboard is bound non-loopback.** At that
point it fails closed until a password or OAuth provider is configured, and
starts using cookies — and the claude-science verdict applies again.

**The dashboard binds loopback and therefore runs with its auth gate off.**
Everything reaching it comes through the gateway and `httpsfront`, so the awm
edge session is the real boundary — the same posture as every other awm page.

## Registrations

Two, from one process:

| kind | name | prefix | what |
|---|---|---|---|
| `service` | `hermes` | `/svc/hermes` | the verbs, plus supervision |
| `url` | `hermes-ui` | `/ui/hermes` | the dashboard, `strip_prefix` on |

The mount dies with this service; the dashboard does not. Registry records are
in memory, so a gateway restart drops the mount and the service's lease loop
puts it back.

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
| `HERMES_MOUNT_PREFIX` | `/ui/hermes` | gateway mount prefix |
| `HERMES_TRANSIENT_UNIT` | `hermes-dashboard` | transient user unit name |
| `HERMES_HEALTH_INTERVAL_S` | `20` | supervision loop period |
| `HERMES_START_TIMEOUT_S` | `180` | how long to wait for the bind |

`HERMES_HOME` is passed explicitly on every invocation rather than inherited: a
service that silently managed a different profile than the one it reports on
would be worse than no service at all.

## Verify

```
awm hermes status                       # listening, pid, unit, mount, model
awm services list | grep hermes         # service + mount registrations
curl -s -o /dev/null -w '%{http_code}\n' $AWM_EDGE_URL/ui/hermes/

# the guarantee
awm hermes status | jq -r .dashboard.pid
awm gateway restart && sleep 20
awm hermes status | jq -r .dashboard.pid   # same pid
```

## Not done here

**Hermes is not a spawnable harness.** `agent_spawn agent_cli="hermes"` would
need an `agentcore/hermes_backend.py` beside `opencode_backend.py`. Config
parity is what "mirrors opencode" meant; the spawner is a separate piece of work
and easy to add later.

**Nothing pins a version.** `hermes update` rewrites the checkout at
`$HERMES_HOME/hermes-agent` in place, and this service supervises whatever that
produces. An update needs an `awm hermes restart` to reach the running
dashboard.
