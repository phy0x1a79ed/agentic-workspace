# Installing the `auth` service (credential authority)

A Python feature service in the `awm.auth` namespace. It is the **single
authority** for AWM's human + machine credentials; the `httpsfront` edge only
*enforces* what this service mints.

What it does:

- Mints a **pair** — a `login-password` (human, typed once a day) and a
  `peer-credential` (machine, for peer nodes) — every **12 h** (cadence),
  each valid **24 h**. The overlapping windows mean up to two generations are
  valid at once, so a client never has to re-authenticate across a rotation.
- On startup mints if none is valid or the newest is older than the cadence, so
  a restart never leaves a stale-only state (no dependency on `events`).
- Signs **sliding session tokens** (HMAC-SHA256 with a long-lived secret); the
  edge verifies + refreshes cookies offline using material from `edge_material`.
- Pushes the day's **login password** (never the peer credential) to Discord
  `#notifications` on each mint, best-effort.
- Mirrors the current peer credential to a file (`$AWM_DIR/services/auth/
  peer_cred.current`) that `$AWM_PEER_CRED` points to, for the SSH peer-auth
  channel (`ssh <peer> 'cat "$AWM_PEER_CRED"'`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`, `persistence`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`) and writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` = the env's absolute interpreter, so the gateway can respawn the
service under systemd's minimal PATH.

## Enable

    awm services enable auth

## CLI

The service's verbs mirror onto the CLI automatically:

    awm auth password     # print the current day's login password + window
    awm auth status       # rotation state summary
    awm auth rotate       # force a fresh mint now

## User accounts

Beside the rotating shared password, static per-user passwords:

    awm auth user-add --username tony      # prints the generated password once
    awm auth user-passwd --username tony   # new password, clears the lockout
    awm auth user-disable --username tony [--disabled false]
    awm auth user-list

`verify` with a `username` mints a session as that user; failures count per username and per client IP and lock the key after `lockout_threshold` attempts for `lockout_minutes`. `AWM_AUTH_PROFILE=public` disables the shared path entirely: no minting, no Discord push, no peer credentials, and a login without a username fails.

## Penpot credentials

Penpot keeps its own accounts and has no "trust the proxy" mode, so this
service also holds one Penpot credential per person. Nobody is ever shown it.

    awm auth penpot-record --username tony --email tony@host --password …
    awm auth penpot-session --username tony        # what the edge asks for
    awm auth penpot-rotate [--username tony]       # force a replacement
    awm auth penpot-list                           # no secrets

`scripts/sirius/add-user.sh` calls `penpot-record` once, right after it creates
the Penpot profile with the same password. A background loop replaces every
stored password at `penpot_rotation_hour` local time, and catches up on start
when the box was off at that hour. This loop runs on the `public` profile too:
that flag turns off the *shared* password, and these are per-user foreign
credentials.

**CAUTION** The stored password is what a rotation offers Penpot as its *old*
password. If the two drift apart, rotation is refused with
`old-password-not-match` and there is no HTTP path back. Re-run
`add-user.sh <name>` on the box holding the stack. It resets the Penpot
password and records the new one, which is the only repair.

## Configuration (settings page / `awm config`)

| Field | Default | Purpose |
|---|---|---|
| `mint_cadence_hours` | `12` | hours between minting a fresh pair |
| `validity_hours` | `24` | hours a pair stays valid (> cadence → overlap) |
| `session_ttl_hours` | `24` | sliding session lifetime (cookie refresh horizon) |
| `max_session_days` | `30` | hard ceiling on total session age |
| `lockout_threshold` | `6` | failed logins per username / client IP before a lock |
| `lockout_minutes` | `15` | how long the lock holds |
| `push_enabled` | `true` | push the login password to Discord on each mint |
| `discord_account` | `discord-bot` | social account id for the push |
| `discord_channel` | `1522674357762261112` | Discord `#notifications` channel id |

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config` | paths + the shared `awm.config.tokens` session-token codec |
| `awm-persistence` | per-service SQLite DB + config contract |
| `awm-gatewayclient` | ServiceAdapter loop + the Discord push RPC (`social.send`) |
| `pydantic` | the config-contract settings model |

## Security notes

- `password`, `peer_credential`, and `edge_material` are effectively
  loopback-only: the edge blocks those paths for any unauthenticated external
  caller, and the loopback gateway is never exposed off-host.
- The signing secret is minted once and never rotated (rotating it would drop
  every live session); the *credentials* rotate instead.
