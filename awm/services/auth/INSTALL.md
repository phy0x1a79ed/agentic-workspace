# Installing the `auth` service (credential authority)

A Python feature service in the `awm.auth` namespace. It holds the material the
`httpsfront` edge enforces with. It holds no login: a person signs in with the
passkey at alcor's approver, and the edge turns that grant into a session
(fleet.md R84, users.md R130, `httpsfront/awm/httpsfront/portal.py`).

What it does:

- Mints a `peer-credential` (the legacy machine bearer for peer nodes) every
  **12 h** (cadence), each valid **24 h**. The overlapping windows mean up to
  two generations are valid at once, so a client never has to re-authenticate
  across a rotation.
- On startup mints if none is valid or the newest is older than the cadence, so
  a restart never leaves a stale-only state (no dependency on `events`).
- Signs **sliding session tokens** (HMAC-SHA256 with a long-lived secret); the
  edge verifies + refreshes cookies offline using material from `edge_material`.
- Mirrors the current peer credential to a file (`$AWM_DIR/services/auth/
  peer_cred.current`) that `$AWM_PEER_CRED` points to, for the SSH peer-auth
  channel (`ssh <peer> 'cat "$AWM_PEER_CRED"'`). That bearer is the legacy
  path. `AWM_PEER_LEGACY_BEARER=0` retires it.
- Holds this node's **Ed25519 key** (`node_ed25519.key`, minted when absent) and
  signs five-minute **node tokens** for other nodes (`sign_peer_token`).
  `node_key` returns the public key and its fingerprint, which a peer pins in its
  book. `edge_material` also hands the edge each book peer with its relation and
  pinned key. See `FEDERATION.md` § *Node identity and tokens*.

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

    awm auth status       # rotation state summary
    awm auth rotate       # force a fresh mint now

`AWM_AUTH_PROFILE=public` turns rotation off: no minting and no peer credentials.

## Device tokens

A device token lets an unattended device or app act as one user without storing a password. It is `awmut1.<id>.<secret>`. This service keeps only the SHA-256 of the secret.

    awm awm device-mint --username tony --label kiosk   # prints the token once
    awm awm device-list [--username tony]               # no secrets
    awm awm device-revoke --id <id>

1. Send the token on every request to the edge as `Authorization: Bearer awmut1.…`. The edge stamps `X-Awm-As: user:<name>` as for a cookie login and sets no cookie.
2. Revoke a lost or retired token by its id. The edge honours a revoked token until its next material refresh, at most 30 s later. Disabling the user drops all of that user's tokens the same way.

Pass `--scope avatar-stage` to narrow a token to the avatar stage session: `POST /svc/avatar/session/stage` and the WebSocket it returns. The edge refuses a scoped token on every other path with a 401. Scope a token whose bearer crosses another host, such as the deneb relay, so a leak there does not hand over the whole user. `awm/service_components/config/awm/config/usertoken.py` holds the shapes, and both this service and the edge read them from there.

**CAUTION** This service sends a scoped token only to an edge that names its scope in `edge_material`. An edge that could not read the scope would honour the token everywhere. A node whose `httpsfront` predates scopes therefore refuses scoped tokens.

The operator, on the host, manages any user's tokens. A signed-in user manages only their own, over HTTP through the edge. `last_used_at` is as coarse as the edge's material refresh: the edge reports use on its next fetch, which the next request after the 30 s window triggers.

## Configuration (settings page / `awm config`)

| Field | Default | Purpose |
|---|---|---|
| `mint_cadence_hours` | `12` | hours between minting a fresh peer credential |
| `validity_hours` | `24` | hours a peer credential stays valid (> cadence → overlap) |
| `session_ttl_hours` | `24` | sliding session lifetime (cookie refresh horizon) |
| `max_session_days` | `30` | hard ceiling on total session age |

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config` | paths + the shared `awm.config.tokens` session-token codec |
| `awm-persistence` | per-service SQLite DB + config contract |
| `awm-gatewayclient` | ServiceAdapter loop |
| `pydantic` | the config-contract settings model |

## Security notes

- `peer_credential`, `edge_material` and `sign_peer_token` are
  effectively loopback-only (the last two also refuse any call that carries an
  identity stamp): the edge blocks those paths for any unauthenticated external
  caller, and the loopback gateway is never exposed off-host.
- The signing secret is minted once and never rotated (rotating it would drop
  every live session); the *credentials* rotate instead.
