# Installing the `2fa` service

A Python feature service in the `awm.twofa` namespace that runs a **local Duo
Mobile device**: it polls Duo during a burst window, auto-approves a lone login,
holds a concurrent burst for manual review, and supports an approve-all window —
the capability ported from the `virtual-auth` daemon that ran on mira, now
in-process with the awm gateway.

> The service/folder name is `2fa` (and the verbs are `2fa_*`); the Python
> package is `awm.twofa` because a module name can't start with a digit.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries it imports (`config`,
`persistence`, `gatewayclient`) and this service into the `awm` env (override
with `AWM_ENV=<name>`), resolving its third-party deps (`pycryptodome` for the
RSA device signature, `requests` for the Duo HTTP calls). It writes a gitignored
`.runtime-env` sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so
the gateway can respawn the service under systemd's minimal PATH.

## Device credentials (provision before the verbs do anything)

The device's secrets live under the workspace runtime dir, **mode 0600, never
committed**:

| File | Purpose |
|---|---|
| `$AWM_DIR/services/2fa/creds.json` | Duo `akey` / `pkey` + host |
| `$AWM_DIR/services/2fa/device_key.pem` | device RSA private key |

Provision them one of two ways:

1. **Copy mira's existing device** (fastest, no Duo-account step):

       mkdir -p "$AWM_DIR/services/2fa"
       scp mira:~/.config/virtual-auth/creds.json      "$AWM_DIR/services/2fa/creds.json"
       scp mira:~/.config/virtual-auth/device_key.pem  "$AWM_DIR/services/2fa/device_key.pem"
       chmod 600 "$AWM_DIR/services/2fa/"*

   (`$AWM_DIR` is `<workspace>/.awm`.) Two hosts then share one device identity,
   which Duo *may* flag — if so, re-enroll a fresh device instead:

2. **Enroll a fresh device** with a new activation code (QR / email value) from
   the Duo account:

       awm 2fa activate "CODE-BASE64HOST"

   This generates a new RSA key, registers the device with Duo, and writes the
   two files above.

Engine/burst tunables are optional env overrides (see `awm/twofa/config.py`):
`AWM_2FA_BURST_WINDOW`, `AWM_2FA_BURST_THRESHOLD`, `AWM_2FA_HOLD_TTL_SECONDS`, …

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name, `2fa`) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the control plane is loopback.

To iterate against a running dev sandbox without installing:

    awm dev shadow --port 7821 awm/services/2fa

(Always pass `--port` to stay off prod `:7819`. If the sandbox has no `2fa` base
yet, the shadow self-registers a journaled base — tear it down afterward with
`AWM_PORT=7821 awm services stop 2fa`, which drops the journal entry first.)

## Verbs

| Verb | Does |
|---|---|
| `2fa_ping` | liveness + enrolled? |
| `2fa_status` | enrolled?, burst active?, held logins, approve-all remaining, approved count |
| `2fa_pending` | list held (burst) logins awaiting a decision |
| `2fa_activate <code>` | enroll a fresh device |
| `2fa_burst [window] [interval] [exit_on_approve]` | open a bounded poll window |
| `2fa_approve <urgid>` | approve a held login |
| `2fa_deny <urgid>` | deny a held login |
| `2fa_approve_all` | open a 5-min approve-all window, clear all held |

## License

This service is a port of the AGPL-3.0 `virtual-auth` project (itself derived
from the reverse-engineered `falsidge/ruo`). The rest of awm is MIT; this dist
carries its own `LICENSE` (AGPL-3.0) — see that file.
