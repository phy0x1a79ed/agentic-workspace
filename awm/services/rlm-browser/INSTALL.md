# Installing the `rlm-browser` service

The first **realm service** in awm's `rlm-*` family — a real Chrome/CDP browser
session pool that web-game effectors drive through the hub. A Python feature
service in the `awm.rlm_browser` namespace. It needs the `awm` conda env to
contain its package plus the shared component libraries it imports (`config`,
`persistence`, `gatewayclient`), and a Chrome/Chromium it can launch (host
backend) or a Docker daemon (docker backend).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name, `rlm-browser`) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/rlm-browser`; it execs this same `run.sh` as an overlay.
A manifest change only takes effect on a **service restart** (the gateway caches
the last `ready` frame) — there is no hot reload.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RLM_BROWSER_BACKEND` | `host` | launch backend for `headless`/`rendered`: `host` (subprocess Chrome) or `docker` (the image below). Ignored by `attached` mode. |
| `RLM_CHROME_BIN` | autodetect | host backend: explicit Chrome/Chromium binary |
| `RLM_ATTACH_CDP_URL` | `http://172.25.176.1:19222` | `attached` mode: CDP endpoint of the existing Chrome to drive. Per-acquire override via `opts.attach_url`. |
| `RLM_BROWSER_IMAGE` | `rlm-browser:latest` | docker backend: image to run |
| `RLM_BROWSER_SHM` | `2g` | docker backend: container `--shm-size` (64 MB default crashes tabs) |

> `attached` mode's default endpoint is the same Chrome the `chrome-devtools`
> MCP server targets (`<workspace>/.mcp.json` → `--browserUrl`). If that
> Windows-side debugging port moves, update both.

Profiles live on the Linux fs at `AWM_DIR/services/rlm-browser/profiles/<game>/`
— keep them on ext4, **never** `/mnt/c` (SingletonLock corruption + perf).

## Contract — relay + live introspection

Functions are projected into the gateway catalog as `rlm_browser_<verb>` tools.
The browser command set is deliberately **not** hand-authored: the act surface is
a single CDP relay, and the command catalog is discovered live from the running
Chrome.

- **lifecycle** — `acquire(game, mode?, opts?) -> {session_id, mode, backend}` ·
  `release(session_id)` · `reset(session_id)` · `status(session_id?)`. `mode`
  (default `headless`) is fixed for the session's life and is one of:
  - **`headless`** — launch Chrome with no display (fast; for bots at scale).
  - **`rendered`** — launch headful + VNC; watchable live (the Docker image).
  - **`attached`** — do **not** launch: drive an *existing* real Chrome over CDP
    (`RLM_ATTACH_CDP_URL`, e.g. the desktop browser — residential fingerprint +
    real logins, so it sidesteps the bot-detection a fresh headless Chrome
    trips). It is **borrowed**: `release` only disconnects (never closes the
    browser), `reset` leaves existing tabs alone, there is **no** per-game
    profile isolation (it's a shared window), and **only one** attached session
    may be active at a time (single-window). A second `acquire(mode=attached)`
    while one is active is rejected.

  (Legacy: a boolean `graphics` is still accepted — `true`→`rendered`,
  `false`→`headless`.)
- **tabs** — `tab_open(session_id, url?) -> {tab_id}` · `tab_close` · `tab_list` ·
  `tab_activate`. A tab is a live CDP page target, not a durable row.
- **perceive** (conveniences over CDP) — `observe(session_id, tab_id?) ->
  {html, url, title}` (rendered, post-JS DOM) · `screenshot(session_id, tab_id?,
  full_page?) -> {image}` (base64 PNG).
- **act (the full set)** — `cdp(session_id, method, tab_id?, params?) -> {result}`
  relays ANY DevTools method to the engine (e.g. `Page.navigate`,
  `Runtime.evaluate`, `Input.dispatchKeyEvent`). Pass `tab_id='__browser__'` for
  browser-level methods.
- **discovery** — `commands(session_id?) -> {protocol}` returns the running
  Chrome's live `/json/protocol` — every domain/method/param reachable through
  `cdp`. This is the dynamic catalog; it re-describes itself across Chrome
  versions with no manifest edit.
- **emitter** — `rlm.browser.<kind>` carrying `{session_id, tab_id, kind, data}`
  (topic `browser`); e.g. `rlm.browser.page_loaded`, `rlm.browser.dialog`,
  `rlm.browser.crashed`. Best-effort live signalling, not durable delivery.

Restart resilience: a session row persists its `cdp_port` + `mode`, so the first
verb after a service restart lazily re-attaches to the still-running Chrome over
CDP — no fresh `acquire`.

## The Docker image (`docker/`)

One image, two modes branched on `RLM_MODE`:

- `headless` — headless Chrome only (lightweight).
- `rendered` — Xvfb → headful Chrome → x11vnc → noVNC, watchable at
  `http://127.0.0.1:<vnc_port>/vnc.html` (the port is in `status`).

Build it:

    docker build -t rlm-browser:latest awm/services/rlm-browser/docker

PID 1 is **supervisord** (mirrors `projects/vpn_bounce`). Chrome's DevTools HTTP
server always binds loopback inside the container (it ignores
`--remote-debugging-address=0.0.0.0`), so a `socat` bridge republishes it on
`0.0.0.0:<cdp_port>`; the host maps that port 1:1 (`-p 127.0.0.1:<port>:<port>`)
so the advertised `webSocketDebuggerUrl` is dialable. Containers run as a
non-root user (Chrome refuses `--no-sandbox` as root).

## T4 — VPN egress (handoff: NOT implemented here)

The `egress` field is a reserved, inert seam: `acquire(opts={"egress": ...})`
stores it and `status` echoes it back; nothing in this service dials a VPN. A
separate agent owns VPN egress and should *extend*, not rewrite, the following:

1. **Sidecar.** Add a `vpn` container — alpine + `openconnect` + supervisord with
   the `quit_on_failure` eventlistener as a kill-switch — exactly as
   `projects/vpn_bounce/main` (`Dockerfile` + `load/supervisord.conf`). Register
   openconnect with `autorestart=false` so a dropped tunnel goes FATAL and brings
   the netns down. This image's own `quit_on_failure` already watches `FATAL`.
2. **Netns share.** Run the Chrome container with `network_mode: service:vpn`
   plus `--cap-add=NET_ADMIN --device=/dev/net/tun` so all its traffic exits
   through the tunnel. CDP/control stay on loopback inside the shared namespace —
   unchanged.
3. **Binding.** Map the `egress` descriptor (resolved at `acquire`) to which VPN
   sidecar a session attaches to; surface the exit identity back through
   `status.egress`.

Footgun for the VPN agent: co-located sessions under a shared exit share one
network namespace, so their CDP ports must be unique across games — the existing
port allocator (`_free_port`) already guarantees this. Per-game exits are a later
roadmap item.

Verification when implemented: `docker exec` the browser container shows the VPN
exit IP (`curl ifconfig.me`) while CDP and the hub control WS stay on loopback.
