# Installing the `vpn` service

The `vpn` feature service owns **VPN egress** for the game-bot composition. It
brings VPN exits up as **singleton containers** — `awm-vpn-ubc` (UBC openconnect)
and `awm-vpn-proton` (Proton, via `protonvpn-cli`) — that other processes route
through by joining the container's network namespace. Each exit is **fail-closed**:
if the tunnel drops, the container (and every consumer's egress) goes down with it.

The Python service is unprivileged — it only orchestrates `docker`. All the
privileged VPN work (openconnect/protonvpn, `NET_ADMIN`, `/dev/net/tun`, routing,
the kill-switch) is isolated **inside** the per-profile image.

Catalog tools: `vpn_up` · `vpn_down` · `vpn_status`.

## 1. Install the Python service

    bash install.sh

Editable-installs the component libraries (`config`, `persistence`,
`gatewayclient`) and this service into the `awm` env (override with `AWM_ENV=<name>`),
and writes a gitignored `.runtime-env` baking `AWM_PYTHON` so the gateway can
respawn it under systemd's minimal PATH.

To iterate without installing, run a worktree's dev sandbox on its own port
(`AWM_PORT=7871 bash core/plugins/dev/sandbox/run.sh start`), NOT dev's :7821.

## 2. Build the per-profile container images

The service runs these images but does **not** build them — build once per host
(and after editing a `containers/<profile>/` file):

    docker build -t awm-vpn-ubc    containers/ubc
    docker build -t awm-vpn-proton containers/proton

The service's user must be able to reach the docker daemon (docker group or
rootless docker). The service adds `--cap-add=NET_ADMIN --device=/dev/net/tun`
itself at `docker run`.

## 3. Credentials — `$AWM_DIR/vpn.toml`

Create `$AWM_DIR/vpn.toml` (chmod 0600 — it holds secrets). One `[<profile>]`
table per exit. Read lazily, only when an `up` runs, so a missing file is fine
until you actually bring an exit up.

    [ubc]
    server        = "myvpn.ubc.ca"
    user          = "cwl@app"            # note the @app realm suffix
    password      = "..."
    second_factor = "push"               # optional; line fed to openconnect's Duo prompt
    twofa_device  = "cwl"                # local awm 2fa device to auto-approve (see § 4); "" disables

    [proton]
    username = "..."
    password = "..."
    server   = ""                        # optional connect target; empty = --fastest

`image` is overridable per profile (defaults `awm-vpn-ubc` / `awm-vpn-proton`).

## 4. 2FA (UBC) — local awm `2fa` Duo auto-approve

UBC requires Duo 2FA. The local awm **`2fa`** service (an in-process Duo
auto-approver) handles it — no more mira/`virtual-auth`. Right before a UBC dial,
the **host-side** `vpn_up` arms a burst on the configured device by POSTing
`POST /invoke {"name": "2fa_burst", "args": {"device": "<twofa_device>"}}` on this same gateway; the burst
auto-approves the *first* pending login within ~1s, so the push `openconnect`
triggers goes through unattended.

- **No token, no network hop:** the call is loopback to our own gateway
  (`$AWM_HUB_URL`); the container itself makes no 2FA call (it can't reach the
  host gateway before the tunnel is up). The arming happens on the cold path of
  `up` only — a no-op `up` on an already-running exit never fires a burst.
- **Device:** `twofa_device` in the `[ubc]` table (default `"cwl"`). The named
  device must be enrolled in the `2fa` service (`awm 2fa devices`). Set
  `twofa_device = ""` to disable (the dial will then hang on Duo unless 2FA is
  otherwise satisfied).
- **Prereq:** the `2fa` service must be running and the device enrolled. Verify:
  `awm 2fa status device=cwl`. The burst window default is ~60s — ample for the
  container start + dial.

## 4a. SSH bounce (ProxyJump) — reaching hosts *behind* the VPN

Each exit also runs a **bounce sshd** in the container's netns, published on the
host's loopback only at `bounce_port` (ubc `2222`, proton `2223`). An
`ssh -W %h:%p` / `ProxyJump` through it egresses via the tunnel. This is how
`ssh sockeye` reaches UBC ARC from every node.

- **Keys:** the bounce admits this node's own bounce key,
  `$AWM_DIR/services/vpn/bounce` (made on first `up`), plus each line of
  `$AWM_DIR/services/vpn/bounce_callers`. The cloud `hpc_ssh` role writes that
  file on altair from the edge keys of every node with a sockeye edge.
- **Routes:** the cloud `hpc_ssh` role renders `vpn_ubc`. On altair it points at
  `localhost:2222` with the bounce key. Elsewhere it adds `ProxyJump altair` and
  the node's edge key.
- **Host keys** are regenerated per container start, so `vpn_ubc` uses
  `StrictHostKeyChecking no` and `UserKnownHostsFile /dev/null`.

## 4b. Keeping the `ubc` exit up

**CAUTION** UBC ends an account's oldest session once it holds about three, so run
the `vpn` plugin on altair only. The service dials `ubc` when it starts and
re-dials when docker reports the container died. Each dial spends a `cwl` Duo
push. After three dials in a row that fail or live under ten minutes, it stops and
pages the operator. `vpn up --profile ubc` starts it again. `vpn down` stops
re-dialing and logs the session out before it removes the container.

## 5. Proton notes

`protonvpn-cli`'s `login` / `connect` / `status` surface is **version-sensitive**.
`containers/proton/dial.sh` uses the common community-CLI shape:
`login <user>` (password on stdin) → `connect [server|--fastest]` → enable
killswitch → a foreground `status` poll that exits (fail-closed) on disconnect. If
your CLI build differs (e.g. the newer official `protonvpn-cli`), adjust those
three invocations and the `status` grep pattern in `dial.sh`, then rebuild the
image. Verify the connect path is non-interactive before relying on it.

## How the gateway picks it up

The gateway discovers any folder with a `run.sh` under `awm/services/`, starts it
with `bash run.sh`, and injects only three env vars (`AWM_HUB_URL`,
`AWM_SERVICE_NAME`, `AWM_SERVICE_ID`). No auth.

## Consuming an exit

`vpn_up` / `vpn_status` return a descriptor with everything a consumer needs:

    {
      "profile": "ubc",
      "container": "awm-vpn-ubc",
      "attach": "--network=container:awm-vpn-ubc",
      "network_mode": "container:awm-vpn-ubc",
      "exit_ip": "...",
      "status": "up",
      "bounce_host": "localhost",
      "bounce_port": 2222,
      "bounce_user": "root",
      "bounce_key": "/home/.../.awm/services/vpn/bounce",
      "usage": "… Route a process through it two ways: netns attach OR SSH ProxyJump …"
    }

- **netns attach** (browsers, docker'd procs): `docker run --network=container:awm-vpn-ubc <image> …`.
- **SSH ProxyJump/-W** (reach a host behind the VPN, e.g. sockeye): tunnel through
  the bounce sshd at `localhost:bounce_port` with the `bounce_key` — see § 4a.
- **A browser session:** `browser acquire{game, opts:{vpn:"ubc"}}` — rlm-browser
  brings the exit up via `vpn_up` and launches Chrome with `--network=container:awm-vpn-ubc`.
  Implementing that `opts.vpn` handling is rlm-browser's job; this service ships the
  descriptor + convention.
