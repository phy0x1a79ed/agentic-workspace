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

To iterate against a running sandbox without installing:

    awm dev shadow --port 7871 awm/services/vpn   # the gamebot sandbox, NOT dev's :7821

> **Do not let this service auto-bootstrap on the canonical dev sandbox (`:7821`)
> or prod.** Discovery is a global filesystem scan and Chrome-behind-VPN egress
> must not come up there by accident. Keep it on `feat/svc-vpn` (or `awm services
> disable vpn`) until the profile-aware discovery gate lands (a separate effort —
> see `feat-gamebot/.awm/context.md` § *Deferred*). Only ever shadow it onto `:7871`.

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
    # virtual-auth on-demand Duo auto-approver on mira (see § 2FA):
    va_burst_url  = "http://10.74.81.111:8077/burst"
    va_token      = "..."                # the [serve] token from mira's virtual-auth config

    [proton]
    username = "..."
    password = "..."
    server   = ""                        # optional connect target; empty = --fastest

`image` is overridable per profile (defaults `awm-vpn-ubc` / `awm-vpn-proton`).

## 4. 2FA (UBC) — virtual-auth Duo auto-approve

UBC requires Duo 2FA. The `virtual-auth` service runs an **on-demand** Duo
auto-approver on mira: you fire a short "burst" that approves the *first* pending
login within ~1s, then exits (no always-on polling). The UBC image's `dial.sh`
does this automatically: it `POST`s `$VA_BURST_URL` with header `X-Token:
$VA_TOKEN` **immediately before** dialing, so the push that openconnect triggers
is the one the burst approves.

- **Endpoint:** `http://10.74.81.111:8077/burst` over ZeroTier (LAN fallback
  `172.16.0.24:8077`). `GET /health` is unauthenticated; `POST /burst` needs the token.
- **Token:** the `[serve] token` in `~/.config/virtual-auth/config.toml` on mira
  (also in `~/.config/va-login.env` on Cosmos). Copy it into `vpn.toml`; treat it
  like a password (it can approve *any* login to the CWL Duo account).
- **Constraints:** one login per burst (the service serializes UBC `up`, and the
  singleton prevents two concurrent UBC dials); the burst→dial window is ~60s; the
  **container** must have ZeroTier reachability to mira's `:8077`. Sanity-check
  first: `curl -fsS http://10.74.81.111:8077/health`.

If `va_burst_url` is omitted, the dial skips the burst and will hang on Duo unless
2FA is otherwise satisfied.

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
      "usage": "… Route any process through it by joining its network namespace …"
    }

- **Any process** (e.g. an ssh client container): `docker run --network=container:awm-vpn-ubc <image> …`.
- **A browser session:** `rlm_browser.acquire{game, opts:{vpn:"ubc"}}` — rlm-browser
  brings the exit up via `vpn_up` and launches Chrome with `--network=container:awm-vpn-ubc`.
  Implementing that `opts.vpn` handling is rlm-browser's job; this service ships the
  descriptor + convention. See the scope's `.awm/context.md`.
