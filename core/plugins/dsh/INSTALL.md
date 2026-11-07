# The `dsh` plugin

## Purpose & Contents

This file holds what a reader cannot recover from the code about the `dsh` plugin: how the DeepSeek harness fork reaches a browser, which harness configuration this repository tracks, where the fork and its node come from, and how to check a node. The harness's own architecture belongs to `projects/deepseek-harness` and its upstream docs.

The plugin is one static binary, `dsh`, which the deploy places at `<AWM_DIR>/bin/dsh`. It needs no install step. It serves tool `dsh` (lifecycle and model route), tool `harness-ds` (harness@1) and page `dsh`.

## How the GUI reaches a browser

The harness is a node web server bound to loopback `12311` with no auth of its own. dsh parents it in its own process group, so it dies with dsh. Its sessions persist under `$DSH_HOME`, so a dsh restart costs a browser reconnect, not a conversation. A `stop` keeps it down until the next `start`.

The path from a browser is: the `auth` origin front on port 12130 (TLS and the edge session), then the gateway's `/s/dsh/`, then dsh's mount, then the fork.

- The fork needs an origin of its own: its frontend builds with an absolute asset base and has no base-path option.
- The fork fences every `/api` call: `Host` must be loopback and a present `Origin` must equal it. The mount lets its client set `Host` from the upstream URL and rewrites `Origin` to `http://127.0.0.1:<port>`.

**CAUTION** The rewrite covers the WebSocket upgrade too. A rewrite on HTTP alone gives a GUI that loads and never streams.

**CAUTION** Keep 12130. A WSL node forwards and firewalls only its awm port range.

The front belongs to `auth`. Until `auth` serves it and its `front` verb, `dsh status` reports `front.serving: false` with the reason, and `dsh url` answers no URL.

The fork's client admits its settings store only on a loopback or an `https:` page. A page through the `auth` front is `https:`. `awm dsh model` steers the model route without the GUI.

## The model route

dsh seeds two sections into `$DSH_HOME/settings.yaml` on start, each only when absent: the `openrouter` provider under `llm-pi-ai`, and the `agent-default-model` selection. Two, because a declared provider that nothing selects still fails every request against the profile's DeepSeek-direct default. A section someone deleted comes back. A section someone edited stays as they left it.

The provider names the key's variable, `OPENROUTER_API_KEY`. dsh reads the value from opencode's `auth.json` at spawn and passes it to the harness. The key never reaches a settings file.

Writes take `settings.yaml.lock`, the lock the fork's `dsh-settings-file` writes under, and never steal it. A write re-renders the document, so comments do not survive.

## Tracked configuration

`config/` holds the hand-authored `$DSH_HOME` content, so every node runs one reviewed configuration. `$DSH_HOME` reaches it through relative symlinks.

| `$DSH_HOME` entry | kind | tracked as |
|---|---|---|
| `.agent-presets` | directory symlink | `config/agent-presets/` |
| `profiles/web/cordis.patch.yml` | file symlink | `config/profiles/web/cordis.patch.yml` |
| `profiles/web/plugins` | directory symlink | `config/profiles/web/plugins/` |
| `settings.yaml` | copy | `config/settings.yaml` |

`settings.yaml` is a copy because the fork's atomic write replaces a symlinked path instead of writing through it. Copy it back into `config/` by hand after changing the route in the GUI.

`config/` lived at `awm/services/dsh/config/` before the Rust port. On each start dsh retargets a `$DSH_HOME` symlink whose target lies in that old folder to the same place under `core/plugins/dsh/config/`. It creates no link and touches nothing else.

**CAUTION** The links point into the deployed checkout, which each deploy resets onto `release`. Commit an edit made through a live link before the next deploy.

Credentials live in `$DSH_HOME/.credentials.yaml`, never in `config/`. `sessions/`, `storages/` and the boot output under `profiles/` are per-node state.

## The fork and its node

`projects/deepseek-harness` forks upstream into `master` (an upstream mirror with no worktree), `dev` (our changes) and `release` (what a node serves). Run `./bootstrap-fork.sh` once per node to create it. No deploy clones it.

dsh launches `<fork>/apps/cli/lib/bin.js` with the node binary in the directory that `<state>/node-bin` names. The service runs under systemd's minimal PATH, which holds no suitable node. The harness needs node 22.19 or later.

How a node builds the fork after the port is an open decision. Until it is settled, build `release` by hand: `pnpm install --frozen-lockfile` and `pnpm run build` under the fork's pinned pnpm. Record the build in `<fork git-dir>/awm/dsh-build-stamp` as `head=<sha> dirty=<0|1> lock=<lockfile hash>`. `dsh status` compares the stamp with the checkout under `source.built_current`.

**WARNING** The fork's session format carries no compatibility promise. Back up `$DSH_HOME` before moving the fork to a new upstream version.

## Verify

1. Run `awm dsh status`.
2. Read `harness.listening`. False with `running` true means the harness failed to bind. Read `awm dsh logs`.
3. Read `model_route`. `provider_declared`, `key_available` and `default_model_ours` fail alike in the GUI and need three different fixes.
4. Read `source`. `dirty` true means an edit in the serving worktree. `built_current` false means the build is older than the checkout.
5. Open the page at `/p/dsh/dsh/`, then **Open harness**. Send one prompt and watch it stream.

## Environment

| Var | Default | Effect |
|---|---|---|
| `DSH_STATE_DIR` | `<AWM_DIR>/services/dsh` | `home/`, `node-bin`, `dsh.log`, `unsent.jsonl` |
| `DSH_HOME` | `<state>/home` | the harness's own data dir |
| `DSH_FORK_DIR` | `<workspace>/projects/deepseek-harness/release` | the fork worktree dsh serves |
| `DSH_UPSTREAM_PORT` | `12311` | the loopback port the harness binds and the mount proxies to |
| `DSH_WORKDIR` | the workspace root | the harness's cwd, so any project is pickable |
| `DSH_HEALTH_INTERVAL_S` | `20` | how often a dead, wanted harness is respawned |
| `DSH_START_TIMEOUT_S` | `60` | how long `start` waits for the port to bind |
| `DSH_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where the OpenRouter key is read from |

To serve the `dev` worktree in a dev sandbox, set `DSH_FORK_DIR` in the sandbox's `.env` (`core/plugins/dev/INSTALL.md`). The gateway passes a service no variable outside the config's `[env]`.
