# dsh

DeepSeek Harness — a plugin-based agent harness whose primary surface is a
browser UI — supervised by awm, wired to OpenRouter, and put on the ZeroTier
mesh behind awm's edge session.

Upstream ships it as a loopback-only web server: it binds `127.0.0.1` by design
and refuses `--host 0.0.0.0` outright. This node is a cloud VM whose only route
to a person is the mesh, so everything here is about crossing that gap without
weakening the posture that makes loopback-only the right default.

## The contract

**The harness is parented, not adopted.** `claude-science` deliberately outlives
its service because a deploy would interrupt an analysis mid-run. This one does
not need that: sessions are persisted under `$DSH_HOME`, so an awm restart costs
a browser reconnect and nothing else. One supervised lifetime covers the whole
stack — no adoption protocol, no orphan to find later. The child gets its own
session so the node process tree can be signalled as a group, and
`PR_SET_PDEATHSIG` closes the window between the fork and the first instruction.

**Why not a gateway `kind=url` mount.** Three independent blockers, not one: the
gateway's url proxy forwards the full request path without stripping the mount
prefix, the harness's frontend is built with an absolute asset base and has no
base-path option, and the gateway's WebSocket bridge forwards no headers at all.
A dedicated front on its own port is the design. Don't re-derive this.

**The `Origin` rewrite is what makes the GUI work at all.** Every `/api` request
passes a browser-trust fence in the harness that compares `Origin` against
`Host` and demands they match; its privileged plane — settings, credentials, the
workspace picker — is additionally pinned to a *loopback* `Host` with an empty
trust list, which no `--trusted-host` grant unlocks. `httpsfront` drops the
inbound `Host` so httpx derives it from the upstream URL, and that is what makes
the harness see a loopback `Host` and open the privileged plane to a remote
browser with its own posture untouched. It forwards `Origin` verbatim, though,
which leaves the fence comparing a mesh origin to a loopback host — 403 on every
call. The front therefore passes `rewrite_origin=True`, an opt-in added to
`httpsfront` for exactly this (see that service's INSTALL.md for why the default
is off). It applies on the WebSocket path too; a rewrite on HTTP alone yields a
GUI that loads and then silently never streams.

**The model route is seeded, not owned.** `$DSH_HOME/settings.yaml` gets two
sections on first start and neither is ever touched again — a route tuned in the
GUI survives every restart and deploy, and deleting a section is how you say
"don't". Two, because declaring a provider is not selecting one: the web profile
ships `agent-default-model = deepseek-official/deepseek-v4-flash`, so a harness
with a perfectly good OpenRouter route still fails every request with
`MISSING_CREDENTIAL` against a key this workspace does not hold. Settings
sections are keyed by the *plugin id* in the composed profile — `llm-pi-ai` and
`agent-default-model`; `dsh --profile web --dump-config` is where those names
come from, and guessing either produces a file the harness reads and ignores.
The credential is *referenced*: the provider block carries
`apiKeyEnv: OPENROUTER_API_KEY`, and the supervisor reads that value out of
opencode's `~/.local/share/opencode/auth.json` at spawn. The key stays in the one
file that already owns it and never reaches a settings file, a git object, or
this service's state.

**The harness is on `compute`'s PROTECTED list.** The gateway inherits the
session id of whichever agent's shell started it, so everything it spawns is
attributable to that agent and therefore reapable. A long-lived node process
that is idle until it streams is exactly the shape of a victim. Only the harness
itself is listed; what it spawns to do work is work.

## Registrations

Two, from one process:

| kind | name | prefix / port | what |
|---|---|---|---|
| `service` | `dsh` | `/svc/dsh` | the verbs, plus supervision |
| — | (TLS front) | `0.0.0.0:12301` | the harness GUI, behind `awm_session` |

The front is not a gateway registration — it is a listener this process owns,
the same shape as `httpsfront`'s own, and it dies with the service.

A third thing appears without this process doing anything: the reception page at
`/ui/dsh`, which the gateway mounts on any node where the page has been *built*.
It reports the harness, the front and the model route as three separate states,
because those are three different failures with three different fixes.

The harness itself listens on loopback `12311`.

## Install

```
./install.sh
```

Idempotent, and it runs on every deploy via `awm/gateway/install.sh`. It installs
the Python bits (including `httpsfront`, whose cert handling, auth gate and
reverse proxy the front is a *configuration* of), writes `.runtime-env`, creates
the `dsh` mamba env pinned to nodejs 24 if it is missing, and installs the
pinned harness into `<workspace>/.awm/services/dsh/runtime/`.

**Node comes from its own env.** The harness needs >= 22.19; the node on the host
PATH is 22.16 and carries no npm, so borrowing it is not an option. The absolute
bin directory is recorded in `.awm/services/dsh/node-bin`, because the supervisor
respawns under systemd's minimal PATH where neither `node` nor the `mamba` that
could find it exists.

**pnpm, not npm.** The harness is a pnpm workspace upstream and `dsh plugin`
shells out to pnpm, but the operative reason is that npm 11's peer resolver does
not converge on this tree in any usable time — the one run that finished needed
`--legacy-peer-deps`, which silently omits `@deepseek-ai/cordis-plugin-group` and
fails at first launch with `ERR_MODULE_NOT_FOUND`. pnpm resolves it in seconds.
Install settings live in `runtime/pnpm-workspace.yaml`: `shamefullyHoist` because
the harness resolves plugin packages by bare name from its own boot bundle, and
an `allowBuilds` list because pnpm 11 will not run a package's build scripts
unless named — without it `node-pty` and `koffi` have no natives and every tool
that touches a subprocess fails at use time while the GUI still loads fine.

**The version is pinned.** dsh is an explicit developer preview that promises
compatibility-breaking changes; bumping `DSH_VERSION` is a deliberate step.

**Certificates are borrowed, not minted.** This node is a deliberate *trust
consumer* — it holds `ca.pem` without `ca-key.pem`, so it must not sign, because
minting would replace the fleet's trust root and surface as a certificate error
on every peer. The leaf `httpsfront` already holds covers the same host and is
port-independent, so the front copies that pair into `<service>/.certs/` when it
has none of its own. Both are gitignored host state, so a deploy's `git clean -fd`
leaves them alone. Nothing re-cuts a consumer's leaf when it expires — check
`front.san` and the expiry if the front ever stops serving.

## Verify

```
awm services list                 # dsh: ready
awm dsh status                    # version, pid, listening, front, model route
```

`status` reports the three states separately on purpose. `harness.listening`
false with `running` true means the process came up and failed to bind — read
`awm dsh logs`. `front.serving` false is a TLS or certificate problem, not a
harness problem. The `model_route` block reports three things that fail the
same way — nothing answers — and need three different fixes:
`provider_declared`, `key_available`, and `default_model_ours`.

Then, from a browser on the mesh:

1. `https://<mesh-ip>:12100/` — the awm landing index lists `dsh`.
2. `/ui/dsh` — the reception page; **Open harness** goes to `https://<mesh-ip>:12301/`.
3. **Settings → Models** opens and saves. This is the test that the `Origin`
   rewrite is actually working: it is the privileged plane, and it 403s without
   it. The route itself is testable without a browser:
   `dsh --profile headless "Reply with exactly the word PONG and nothing else."`
   with `DSH_HOME` and `OPENROUTER_API_KEY` set.
4. Send one prompt and watch it *stream*. A response that arrives in one lump, or
   not at all, is the WebSocket path — a rewrite that reached HTTP and not WS.

A device that has never talked to awm needs the root once:
`https://<mesh-ip>:12301/ca.crt`.

## Environment

| Var | Default | Effect |
|---|---|---|
| `DSH_STATE_DIR` | `<workspace>/.awm/services/dsh` | runtime, `$DSH_HOME`, log, `node-bin` |
| `DSH_HOME` | `<state>/home` | the harness's own data dir |
| `DSH_UPSTREAM_PORT` | `12311` | loopback port the harness binds |
| `DSH_FRONT_PORT` | `12301` | mesh TLS port |
| `DSH_WORKDIR` | the workspace root | the harness's cwd, so any project is pickable |
| `DSH_NODE_ENV` | `dsh` | mamba env holding node (install-time only) |
| `DSH_VERSION` | pinned in `install.sh` | harness version to install |
| `DSH_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where the OpenRouter key is read from |
