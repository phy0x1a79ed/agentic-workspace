# claude-view

Fleet observability for the Claude Code agents this workspace runs. The upstream
project ([tombelieber/claude-view](https://github.com/tombelieber/claude-view),
MIT) is a local-first dashboard over the JSONL session files under
`~/.claude/projects/` — per-session cost, context-window gauges, sub-agent
trees, full-corpus search, plus a control plane (create/kill CLI sessions, a
tmux-backed web terminal, resolving pending permission prompts) that FleetView
does not have.

This service wraps it: awm spawns and supervises the binary, and puts an
authenticated TLS front on the mesh in front of it.

## Install

```
./install.sh              # python bits + build the binary if not staged
./install.sh --rebuild    # force a rebuild of the binary
```

The first run builds a Docker image and compiles the Rust server; budget ~10
minutes. Afterwards the cargo cache under `vendor/cargo/` makes rebuilds much
faster.

## Why we compile instead of downloading the release

Upstream publishes prebuilt `linux-x64` binaries and the intent was to consume
them. **They do not run on this host.** CI builds them on `ubuntu-24.04`, so
they carry `GLIBC_2.38`/`GLIBC_2.39` version requirements; this box is Ubuntu
22.04 with glibc 2.35. The loader rejects the binary on the version-set check,
before symbol resolution — there is no flag, env var, or `LD_PRELOAD` shim that
gets past it. Only four symbols are actually too new (`pidfd_spawnp`,
`pidfd_getpid`, `__isoc23_sscanf`, `__isoc23_strtol`).

So `docker/build.sh` compiles the pinned tag itself. **This is not a fork** — it
is the pinned tag plus one reviewable patch (see *Patches* below):

- the source is a shallow clone at tag `v0.45.0`, and every build does
  `reset --hard` to the tag before re-applying `docker/patches/*.patch`, so what
  is compiled is always exactly "tag + those patches" and a hand-edit made while
  debugging cannot survive into a build;
- the compiler is *upstream's own* — the repo pins `channel = "1.94.1"` in
  `rust-toolchain.toml` and rustup honours it inside the container, so the
  image's Rust tag is only a bootstrap;
- the build command is the one from upstream's `release.yml`;
- the frontend is the **official** prebuilt `dist/` (plus `sidecar/`) lifted
  from the release tarball, checksum-verified against upstream's
  `checksums.txt`. Only the Rust server is rebuilt, because the frontend is not
  embedded in the binary — `crates/server/src/startup/paths.rs` resolves
  `./dist` beside the executable at runtime. That keeps bun and node out of the
  picture entirely.

`vendor/v<version>/BUILD_INFO` records the source SHA, the patches applied and
the files they touched, the target, and the binary hash for every build.

### Patches

`docker/patches/` — applied in filename order, and each one is expected to be
small enough to read in full.

- **`0001-pid-snapshot-honour-data-dir.patch`** — makes `pid_snapshot_path()`
  honour `CLAUDE_VIEW_DATA_DIR` instead of hardcoding
  `$HOME/.claude/live-monitor-pids.json`. Upstream's own
  `crates/core/src/paths.rs` documents that variable as the single source of
  truth for *all* write paths, so this is a bug fix rather than a customisation
  — it falls back to the historical location when the variable is unset, so
  default single-instance behaviour is unchanged. Without it the file escapes
  the data dir, which means a dev and a prod instance on this host would
  silently share and clobber one snapshot. Worth offering upstream.

The build targets `x86_64-unknown-linux-musl`, where Rust links `crt-static` by
default, so the result is a **fully static binary with no libc dependency**.
That makes the glibc-floor problem permanently moot rather than re-pinning us to
whatever this box happens to run. Alpine's native target is musl, so this is a
native build, not a cross — none of the linker/`CC`/`AR` juggling that
multi-target cross builders need.

### Side effect: telemetry is structurally impossible

Upstream CI injects `POSTHOG_API_KEY`, `SUPABASE_*`, `RELAY_URL` and `SHARE_*`
from repo secrets at compile time via `option_env!`. We supply none of them.
`resolve_status_pure` returns `Disabled` when there is no compile-time key, so
telemetry cannot be switched back on by an environment variable — there is
nothing to switch on. Cloud sync, relay and share are inert for the same reason.
`CLAUDE_VIEW_TELEMETRY=0` is still set at launch so the intent survives if we
ever move to an official binary.

## Version bumps

Pinned to **v0.45.0**. Upstream is fast-moving (50+ releases, near-daily
pushes), so a bump is a deliberate step:

1. `CLAUDE_VIEW_VERSION=<new> ./install.sh --rebuild`
2. **Re-run the corpus gate** and compare against the baseline below. The gate
   is what caught the containment bug documented under *Known upstream quirks*.

### Corpus-gate baseline (v0.45.0, this host)

Measured against 3.8 GB / 3,939 JSONL files (1,726 top-level sessions + 2,213
subagent transcripts, 284 projects). Both columns are the same source at the
same tag, differing only in libc — the glibc column is kept because it is the
reference for how much the musl allocator costs:

| | musl (**shipping**) | glibc (reference) |
|---|---|---|
| Cold index | 13.1 s | 1.61 s |
| Server ready | 2.3 s | 6.7 s |
| Peak RSS | **348 MB** | 706 MB |
| Steady RSS | **~130 MB** | ~650 MB |
| Index on disk | 25 MB | 26 MB |
| Full-corpus search | 14–25 ms | 6–9 ms |
| Sessions / projects | 1,725 / 284 | 1,721 / 284 |

The 8× index regression is musl's allocator under the parallel indexer, and it
was accepted deliberately: it is a **one-time cold pass** at boot, after which
the file watcher goes incremental, and it buys a 4–5× cut in steady-state
resident memory for a service that runs permanently. Search stays imperceptible.
If that trade ever stops being worth it, switch the Dockerfile base to
`rust:<ver>-bullseye` (glibc 2.31, low enough to run here) and rebuild.

Correctness spot checks matched the filesystem exactly: 284/284 projects,
`awm-dev` 123↔123, `scadc-metabolic-modelling` 54↔54. The session count runs a
handful below the 1,726 files on disk because some session files contain no
user/assistant turns at all.

Containment verified: with the patch applied, **nothing** is written outside
`CLAUDE_VIEW_DATA_DIR` — `settings.json` byte-identical, no hooks, no
statusline, no `~/.claude-view/`, no stray files under `~/.claude`. Zero
outbound connections.

## Runtime layout

| Path | What |
|---|---|
| `vendor/v<version>/` | binary + official `dist/` + `sidecar/` (gitignored) |
| `vendor/src/` | pinned upstream clone (gitignored) |
| `vendor/cargo/` | cargo download cache (gitignored) |
| `state/` | SQLite index, tantivy segments, port file, logs (gitignored) |
| `.certs/` | minted TLS leaf for the mesh front (gitignored) |
| `.sans` | operator-declared extra SANs, one per line (gitignored) |

## Node is a hard runtime dependency

The Rust server is not self-contained. Its `/api/sidecar/*` routes and the
`/ws/chat/*` relay — everything behind the **Chat** tab — are proxies to a Node
sidecar that the server spawns on demand as literally `Command::new("node")`,
resolved from `PATH`. The gateway runs services on systemd's minimal `PATH`,
where `node` does not exist, so the service resolves it explicitly (`node_path()`
in `binary.py`) and prepends its directory to the child's `PATH`. It prefers the
`node` sitting beside our own interpreter — the awm conda env ships one — so the
dependency is satisfied by the same install that provides python.

Getting this wrong fails *quietly and misleadingly*: the SPA loads, the terminal
works (that relay is Rust-native), and only chat breaks — a 503 with the send
button greyed out and the chat list stuck on "Loading...". Upstream's circuit
breaker then counts the failed spawn **before** it checks whether `node` exists,
so ten dead attempts in forty seconds latch it open and every later request
reports "circuit open" instead of the real cause. `status` reports
`sidecar_capable` so this is visible without reading logs.

## Ports

| Port | Bind | What | Override |
|---|---|---|---|
| 47892 | `127.0.0.1` | upstream claude-view server — **loopback only** | `CLAUDE_VIEW_UPSTREAM_PORT` |
| 12110 | `0.0.0.0` | TLS front, awm edge auth | `CLAUDE_VIEW_FRONT_PORT` |
| 3001 | `127.0.0.1` | Node sidecar, spawned by the server — **forced**, see below | `CLAUDE_VIEW_SIDECAR_PORT` |

12110 sits inside the `12100..12150` band that `/mnt/a/linux/ssh_settings.ps1`
already forwards wholesale through the Windows portproxy, so no elevated re-run
of that script is needed (unlike Claude Science's 12201/12202).

**The sidecar's bind is forced, because upstream offers no knob.** Its bundle
calls `.listen(SIDECAR_PORT)` with no host argument, so Node binds every
interface, and it reads no host/bind variable. That matters because the sidecar
is the unauthenticated CLI control bridge: whatever reaches it can spawn and
drive Claude Code sessions. It was never mesh-reachable — the portproxy forwards
only `12100..12150` — but it *was* reachable from the Windows host on the WSL
interface, and "safe because an unrelated config file happens not to forward
that port" is not a security boundary.

`node/loopback-listen.cjs` patches `net.Server.prototype.listen` to supply
`127.0.0.1`, preloaded via `NODE_OPTIONS=--require` from `child_env()`. The
preload runs before the bundle is evaluated, so the vendored release asset —
which is checksum-verified against upstream's `checksums.txt` — stays untouched
and needs no re-patching on a version bump. `net` rather than `http` because
`http.Server` extends `net.Server` and delegates `listen()` to it, so one seam
covers HTTP, HTTPS and raw TCP. Verify after any bump with
`ss -lntp | grep 3001`: it must read `127.0.0.1:3001`, never `*:3001`.

`CLAUDE_VIEW_BIND_ADDR` is deliberately left unset and actively stripped from
the child environment. The upstream server defaults to `127.0.0.1`, and
loopback-only is the entire security model: the mesh reaches it *only* through
the authenticated front.

## Running a dev instance alongside prod

Prod runs permanently under the systemd gateway while this service is still
being worked on, so a second instance from a worktree is the normal case, not an
edge case. Four things are per-instance, and all four are already parameterised:

| What | Knob | Notes |
|---|---|---|
| server port | `CLAUDE_VIEW_UPSTREAM_PORT` | |
| front port | `CLAUDE_VIEW_FRONT_PORT` | stay inside `12100..12150` to keep the portproxy |
| index + state | `CLAUDE_VIEW_STATE_DIR` | already per-tree: defaults beside the source |
| sidecar port | `CLAUDE_VIEW_SIDECAR_PORT` | **must differ — see below** |

Two of those are not ordinary collisions, and both fail destructively rather
than loudly:

- **The sidecar port is a kill, not a bind failure.** Before spawning its
  sidecar the server runs `kill_port_holder(port)`, which lsofs the port and
  `kill -9`s any *node* process holding it. A second instance left on the
  default 3001 does not fail to start — it SIGKILLs the first instance's
  sidecar and takes over, so prod's chat surface dies with no error on prod's
  side. Always give a dev instance its own.

- **The hooks are a singleton, and claiming them is total.** They name one port,
  so agent state can only flow to one instance; `install()` therefore strips
  *every* claude-view entry rather than just its own. If a dev instance wrote
  `~/.claude/settings.json` it would take prod's hooks away, and `remove()` —
  scoped to its own port — would not give them back on the way out. Prod would
  then run with no agent state at all, which surfaces as the Live Monitor
  reporting the whole fleet as "Needs You" rather than as any kind of error.

  So ownership is structural (`hooks.owns_fleet_settings`): only the instance
  registered with the **prod gateway** (`AWM_HUB_URL` on `:7819`) writes the
  fleet-wide file. A dev sandbox, or a standalone run with no hub at all, writes
  `settings.dev.json` inside its own state dir instead. It fails safe — a
  misconfigured dev instance loses *its own* agent state, never prod's — and
  `status.hooks.fleet_wide` reports which side of that line an instance is on.

  The cost is that a dev instance shows every session as "Needs You", because
  nothing reads its scratch file. To actually exercise hooks in dev, stop prod
  first and point the dev instance at the real file explicitly with
  `CLAUDE_VIEW_SETTINGS=~/.claude/settings.json`.

## Hooks: we register them, claude-view does not

Agent state — "is this agent working, or waiting on me?" — has exactly one
source. `routes/hooks/resolve_state.rs` calls itself "the SOLE authority for
agent state" and means it: state exists only because Claude Code hooks POST to
`/api/live/hook`. With no hooks, every session resolves to `state: "unknown"`,
which the UI buckets as **"Needs You"** — so the Live Monitor cheerfully reports
that seven agents are awaiting input while all seven are working. That is the
one question this service exists to answer, so the hooks are not optional.

Upstream registers them for us, but `register_hooks()` also injects a
**statusline**, and the two cannot be separated — `CLAUDE_VIEW_SKIP_HOOKS=1`
disables both. The statusline is the problem: it is a *single slot* in
`settings.json`, not an array, so a dev and a prod instance would silently
overwrite each other's, and the wrapper script it writes lands in
`~/.claude-view/`, outside `CLAUDE_VIEW_DATA_DIR` and therefore outside every
containment guarantee the corpus gate verified.

So the flag stays set and `awm/claude_view/hooks.py` does the half we want:

- **25 observation events, never `WorktreeCreate`.** Claude Code has two kinds
  of hook. Observation hooks fire alongside the action and their stdout is
  discarded. Replacement hooks *replace* the action and Claude Code parses their
  stdout as the result. `WorktreeCreate` is a replacement hook — registering it
  as an observer makes it return `{"ok":true}` where a worktree path belongs, so
  every `isolation: "worktree"` subagent call on the host fails, silently and
  nowhere near the cause. `install()` asserts the two sets are disjoint before
  writing a byte.
- **Upstream's sentinel** (`# claude-view-hook`) and its exact command string.
  If anyone ever drops `CLAUDE_VIEW_SKIP_HOOKS`, claude-view's own cleanup pass
  recognises and reclaims our entries rather than stacking a second copy beside
  them. One namespace either way.
- **User entries are never touched.** Only sentinel-bearing matcher groups are
  added or removed; anything hand-authored is matched by neither branch.
- **Atomic writes.** Rendered, re-parsed, then `os.replace`d. Claude Code skips
  a settings file with a JSON error *entirely* — not just the bad key — so a
  torn write would disable every setting on the host.
- **Asymmetric lifecycle.** `install()` clears *all* claude-view entries then
  adds its own, because a stale instance's hooks curl at a dead port on every
  tool call of every agent. `remove()` is scoped to our own port, so shutting
  one instance down never rips out hooks another has since installed.
- **No statusline, ever.**

`status` reports what is actually in the file, so a partial registration is
visible without reading JSON by hand. Sessions pick the hooks up at *session
start*: already-running agents keep reporting `unknown` until they next launch.

## Auth

The front reuses `awm.httpsfront` wholesale — `certs.ensure_certs` (a leaf off
the shared remote-audio CA, so devices already trusting awm need nothing new),
`auth.AuthGate` (verifies the `awm_session` cookie offline via HMAC, fails
closed), and `proxy.serve` (HTTP + WebSocket bridging). The only change upstream
of us was adding a `landing=False` flag to `proxy.serve()`, because the gateway
front owns `/` with an index of `/ui/*` pages and here `/` belongs to
claude-view's SPA.

Cookies are scoped by host and ignore port, so logging in once at `:12100`
authenticates `:12110` too — no second sign-in.

**There is deliberately no `/_signin` convenience route.** Claude Science has
one, and it auto-authorizes any device that reaches the mesh. This front serves
the fleet's complete conversation transcripts; that is a materially larger blast
radius, and the edge auth is what makes exposing it acceptable at all.

## Known upstream quirks

- **`~/.claude/live-monitor-pids.json`** — **patched out**, see *Patches*. This
  was the one write that escaped `CLAUDE_VIEW_DATA_DIR`; the corpus gate is what
  caught it. It now lands in `state/` alongside the index, so dev and prod
  instances stay separate.
- **Reads `~/.claude/.credentials.json`.** The `/api/oauth/usage` route uses the
  Claude Code OAuth token to call Anthropic's usage API for the plan and
  rate-limit display. The token is not exposed through claude-view's own API,
  but it is a real capability sitting behind this front.
- **`processCount` is always 0.** Not host-specific:
  `count_claude_processes()` only counts a process when `process.cwd().is_some()`,
  but it refreshes via sysinfo 0.33's `refresh_processes()`, whose default
  `ProcessRefreshKind` requests neither `cwd` nor `cmd`. So `cwd` is `None` for
  every process and the count cannot be anything but zero. The `cmd` half of the
  name test is dead for the same reason. Impact is nil — the scan's only consumer
  is this display metric (`ClaudeProcess`, `find_process_for_project` and
  `has_running_process` have no callers), and session/project attribution comes
  from the JSONL `entrypoint` instead. Worth offering upstream as a one-line fix.
- **The process-name test would miss modern Claude Code anyway.** It matches
  `name().contains("claude")` or an arg containing `@anthropic-ai/claude`.
  Claude Code 2.1.x runs sessions from `~/.local/share/claude/versions/<ver>`,
  so `comm` is the bare version string (`2.1.217`) and argv carries
  `claude bg-spare --bg-spare …` — neither test matches. Only the daemon
  (`comm=claude`) would.
- **The `private` submodule.** `.gitmodules` declares `claude-view-private`,
  which is not public. The server target does not need it and the build never
  initialises submodules, but it means a from-source build of the *full*
  workspace is an unproven path.
