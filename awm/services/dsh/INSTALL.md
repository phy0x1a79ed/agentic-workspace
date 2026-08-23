# dsh

DeepSeek Harness — a plugin-based agent harness whose primary surface is a
browser UI — supervised by awm, wired to OpenRouter, and put on the ZeroTier
mesh behind awm's edge session.

Upstream ships it as a loopback-only web server. It binds `127.0.0.1` by design
and refuses `--host 0.0.0.0` outright. This node is a cloud VM whose only route
to a person is the mesh, so everything here is about crossing that gap without
weakening the posture that makes loopback-only the right default.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why the
harness sits behind a dedicated front instead of a gateway mount, why a mesh
page is trusted for settings, and how a forked source tree becomes a served GUI.

The harness's own architecture belongs to `projects/deepseek-harness` and its
upstream docs. Each scope's `.awm/context.md` there says which branch does what.
This file covers only the boundary between awm and the harness.

## The contract

**The harness is parented, not adopted.** `claude-science` deliberately outlives
its service because a deploy would interrupt an analysis mid-run. This one does
not need that: sessions are persisted under `$DSH_HOME`, so an awm restart costs
a browser reconnect and nothing else. One supervised lifetime covers the whole
stack — no adoption protocol, no orphan to find later. The child gets its own
session so the node process tree can be signalled as a group, and
`PR_SET_PDEATHSIG` closes the window between the fork and the first instruction.

**Why not a gateway `kind=url` mount.** Three independent blockers, not one:

- The gateway's url proxy forwards the full request path without stripping the
  mount prefix.
- The harness's frontend builds with an absolute asset base and offers no
  base-path option.
- The gateway's WebSocket bridge forwards no headers at all.

A dedicated front on its own port is the design. Don't re-derive this.

**The `Origin` rewrite is what makes the GUI work at all.** Every `/api` request
passes a browser-trust fence in the harness: the `Host` must be loopback or a
`--trusted-host` grant, and a present `Origin` must equal it. `httpsfront` drops
the inbound `Host` so httpx derives it from the upstream URL, which satisfies the
first half without any grant. It forwards `Origin` verbatim, though, which leaves
the fence comparing a mesh origin to a loopback host — 403 on every call,
handshakes included. The front therefore passes `rewrite_origin=True`, an opt-in
added to `httpsfront` for exactly this (see that service's INSTALL.md for why the
default is off). It applies on the WebSocket path too. A rewrite that reaches
HTTP alone yields a GUI that loads and never streams.

**Settings work over the mesh because the fork widened one client decision.**
The fence above is a server check and the rewrite satisfies it. The harness's
*client* makes a second, independent decision: which backing store the settings
mirror uses. Upstream picks `host` only when `location.hostname` is loopback, so
on a mesh address the mirror stayed empty, the Models page reported "settings are
unavailable in this browser", and the Plugins page rendered no cards. That is
`location`, not a header, so no proxy setting reaches it.

The fork adds `ConnectionHandle.settingsTrusted`, which also admits an `https:`
page. The harness serves plain HTTP on loopback, so an `https:` page can only
have arrived through this service's TLS front, which authenticates every request
against the edge session first. Three things stay narrow on purpose:

- `isLoopback` keeps its meaning, so the native "open this file" affordance stays
  off over a network. It would open a file on this VM's absent desktop.
- The server-side `/api` Host fence is untouched.
- A plain-HTTP page on a non-loopback host is still refused.

The route also has a verb, which is what to reach for without a browser:
`awm dsh model` reports the selection and the route's catalog, and
`awm dsh model --model <id>` changes it. `dsh-settings-file` watches the document
and hot-publishes external edits, so that applies without a restart. Writes take
the same `<file>.lock` the harness writes under, and never steal it.

**The model route is seeded, not owned.** `$DSH_HOME/settings.yaml` gets two
sections on first start and neither is ever touched again — a route tuned in the
GUI survives every restart and deploy, and deleting a section is how you say
"don't". Two, because declaring a provider is not selecting one: the web profile
ships `agent-default-model = deepseek-official/deepseek-v4-flash`, so a harness
with a perfectly good OpenRouter route still fails every request with
`MISSING_CREDENTIAL` against a key this workspace does not hold. Settings
sections are keyed by the *plugin id* in the composed profile — `llm-pi-ai` and
`agent-default-model`. `dsh --profile web --dump-config` is where those names
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
itself is listed. What it spawns to do work is work.

CAUTION: the entry matches the launcher path
`deepseek-harness/…/apps/cli/lib/bin.js`, because nothing on the command line is
called `dsh` any more. Changing how `harness.py` spawns the child without
changing that pattern makes the harness reapable again, and nothing reports it.

## Registrations

Two, from one process:

| kind | name | prefix / port | what |
|---|---|---|---|
| `service` | `dsh` | `/svc/dsh` | the verbs, plus supervision and the model route |
| — | (TLS front) | `0.0.0.0:12301` | the harness GUI, behind `awm_session` |

The front is not a gateway registration — it is a listener this process owns,
the same shape as `httpsfront`'s own, and it dies with the service.

A third thing appears without this process doing anything: the reception page at
`/ui/dsh`, which the gateway mounts on any node where the page is *built*.
It reports the harness, the front and the model route as three separate states,
because those are three different failures with three different fixes.

The harness itself listens on loopback `12311`.

## Install

```
./install.sh
```

`awm/gateway/install.sh` runs this on every deploy. Every step is idempotent and
skips itself when already satisfied. It does four things:

- Installs the Python bits, including `httpsfront`, whose cert handling, auth
  gate and reverse proxy the front is a *configuration* of.
- Writes `.runtime-env`, the absolute interpreter the supervisor respawns with.
- Creates the `dsh` mamba env pinned to nodejs 24 when it is missing.
- Builds the harness fork.

**The harness is a project, not a dependency.**

```
./bootstrap-fork.sh          # once per node
```

`projects/deepseek-harness` forks `deepseek-ai/deepseek-harness` into three
branches. `master` mirrors upstream and carries no worktree. `dev` carries every
change we author. `release` is what a deployed node serves. `bootstrap-fork.sh`
creates all three and restores the `master` branch that retiring its worktree
destroys. Run the script rather than the commands inside it.

`install.sh` never creates the fork, because cloning 109 MB is not something a
deploy may do behind your back. It warns instead, the service registers, and
`status` reports the harness unbuilt. The gateway runs every service's install
under `set -e`, so failing here aborts the whole deploy on a node that simply
does not serve the harness. `DSH_REQUIRE_HARNESS=1` makes it fatal on a node
that is supposed to have one.

**A worktree is not a checkout of a harness. It is a runnable harness.**
`pnpm install --frozen-lockfile` followed by `pnpm run build` produces
`apps/cli/lib/bin.js`, the CLI `harness.py` launches. Everything untracked inside
the worktree — `node_modules/`, each package's `lib/` — derives from tracked
files, so the deployed state and the repository state are the same thing. A
change to the harness is therefore a commit, not an edit to a build artifact
nobody tracks. That is the whole reason for the fork.

The chain from an edit to a served GUI:

1. Edit in `dev`. `pnpm run dev:web` rewrites each package's `lib/client.js`, and
   the harness's HMR broadcasts a `rebuilt` frame over `GET /plugins/events`, so
   the edit reaches an open browser in about ten seconds with no restart.
2. Run `git -C ../release merge dev`. That merge is the transaction.
3. Run `install.sh`. It rebuilds `release` and stamps the build.
4. `harness.py` spawns `node <release>/apps/cli/lib/bin.js --profile web` on
   loopback. The front puts it on the mesh.

**Builds are stamped, not sniffed.** `<fork>/.awm/dsh-build-stamp` records the
commit, the dirty flag and the lockfile hash the current `lib/` came from. A
deploy whose tree stayed put costs 0.1 s instead of a 45 s incremental build or a
2.5 min cold one. `awm dsh status` reports the same three facts under `source`,
so a stale or hand-edited deployment is visible rather than something you infer
from a symptom. `DSH_SKIP_BUILD=1` opts out.

**Node comes from its own env.** The harness needs >= 22.19. The node on the host
PATH is 22.16 and carries no npm, so this env is the only source. The absolute
bin directory is recorded in `.awm/services/dsh/node-bin`, because the supervisor
respawns under systemd's minimal PATH where neither `node` nor the `mamba` that
could find it exists.

**pnpm, not npm.** The harness declares `packageManager: pnpm@11.7.0` and is a
pnpm workspace of ~250 projects. npm 11's peer resolver does not converge on this
tree in any usable time — the one run that finished needed `--legacy-peer-deps`,
which silently omits `@deepseek-ai/cordis-plugin-group` and fails at first launch
with `ERR_MODULE_NOT_FOUND`. The pnpm `install.sh` puts in the node env is only a
bootstrap: it reads `packageManager` and hands off to the pinned version, so the
build runs under upstream's toolchain. Install settings come from the workspace's
own `pnpm-workspace.yaml`, whose `allowBuilds` already names `node-pty` and
`koffi` — without their natives every tool that touches a subprocess fails at use
time while the GUI still loads fine.

CAUTION: a bare `pnpm install` in a fork worktree fails. `install.sh` sets the
one environment variable that avoids it, and explains why at that line. Copy the
command from there before running pnpm by hand.

**The version moves by merge.** dsh is an explicit developer preview that
promises compatibility-breaking changes. Take a new one by fetching `upstream`
and merging `upstream/master` into `dev`, which puts our edits in the conflict
path where they can be reviewed, then promote `dev` to `release`.

WARNING: `dsh-session` keeps `SESSION_FORMAT_VERSION` at `0` with no
compatibility promise, so back up `$DSH_HOME` before a version move.

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

`status` reports the states separately on purpose. `harness.listening`
false with `running` true means the process came up and failed to bind — read
`awm dsh logs`. `front.serving` false is a TLS or certificate problem, not a
harness problem. The `model_route` block reports three things that fail the
same way — nothing answers — and need three different fixes:
`provider_declared`, `key_available`, and `default_model_ours`. `source.dirty`
true means somebody edited the serving worktree, and `source.built_current`
false means the built bundles are older than the commit that is checked out.

Then, from a browser on the mesh:

1. `https://<mesh-ip>:12100/` — the awm landing index lists `dsh`.
2. `/ui/dsh` — the reception page. **Open harness** goes to `https://<mesh-ip>:12301/`.
3. Open **Settings → Models**. It must list the provider directory with an
   `openrouter` row. **Settings → Plugins** must render configuration cards.
   Both empty, or "settings are unavailable in this browser", means the page did
   not reach `settingsTrusted` — check that the address really is `https:`.
4. Send one prompt and watch it *stream*. A response that arrives in one lump, or
   not at all, is the WebSocket path — a rewrite that reached HTTP and not WS.

A device that has never talked to awm needs the root once:
`https://<mesh-ip>:12301/ca.crt`.

## Environment

| Var | Default | Effect |
|---|---|---|
| `DSH_FORK_DIR` | `<workspace>/projects/deepseek-harness/release` | the harness worktree this node builds and serves |
| `DSH_STATE_DIR` | `<workspace>/.awm/services/dsh` | `$DSH_HOME`, log, `node-bin` |
| `DSH_HOME` | `<state>/home` | the harness's own data dir |
| `DSH_UPSTREAM_PORT` | `12311` | loopback port the harness binds |
| `DSH_FRONT_PORT` | `12301` | mesh TLS port |
| `DSH_WORKDIR` | the workspace root | the harness's cwd, so any project is pickable |
| `DSH_NODE_ENV` | `dsh` | mamba env holding node (install-time only) |
| `DSH_SKIP_BUILD` | unset | `1` leaves the fork's build alone (install-time only) |
| `DSH_REQUIRE_HARNESS` | unset | `1` makes a missing fork fatal (install-time only) |
| `DSH_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where the OpenRouter key is read from |

Point `DSH_FORK_DIR` at `projects/deepseek-harness/dev` in a sandbox's gitignored
`awm/gateway/dev/.env` to run the hot loop against a supervised service.
