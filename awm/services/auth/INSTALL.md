# Installing the `auth` service (credential authority)

A Python feature service in the `awm.auth` namespace. It holds the material the
`httpsfront` edge enforces with. It holds no login: a person signs in with the
passkey at alcor's approver, and the edge turns that grant into a session
(fleet.md R84, users.md R130, `httpsfront/awm/httpsfront/portal.py`).

What it does:

- Holds the long-lived HMAC secret the edge signs **sliding session tokens**
  with. The edge verifies and refreshes cookies offline, using material from
  `edge_material`.
- Holds the operator's **device tokens** (below).
- Holds no credential for other nodes: a node reaches another over the ssh edge
  (fleet.md R92), not through the HTTPS front.

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

    awm auth status       # session lifetime and live device tokens

## Device tokens

A device token lets an unattended device or app act as one user without storing a password. It is `awmut1.<id>.<secret>`. This service keeps only the SHA-256 of the secret.

    awm auth device-mint --label kiosk --scope avatar-stage   # prints the token once
    awm auth device-list                                      # no secrets
    awm auth device-revoke --id <id>

1. Send the token on every request to the edge as `Authorization: Bearer awmut1.…`. The edge stamps `X-Awm-As: user:<name>` as for a cookie login and sets no cookie.
2. Revoke a lost or retired token by its id. The edge honours a revoked token until its next material refresh, at most 30 s later.

Pass `--scope avatar-stage` to narrow a token to the avatar stage session: `POST /svc/avatar/session/stage` and the WebSocket it returns. The edge refuses a scoped token on every other path with a 401. Scope a token whose bearer crosses another host, such as the deneb relay, so a leak there does not hand over the whole user. `awm/service_components/config/awm/config/usertoken.py` holds the shapes, and both this service and the edge read them from there.

**CAUTION** This service sends a scoped token only to an edge that names its scope in `edge_material`. An edge that could not read the scope would honour the token everywhere. A node whose `httpsfront` predates scopes therefore refuses scoped tokens.

Only the operator, on the host, mints, lists and revokes tokens. `last_used_at` is as coarse as the edge's material refresh: the edge reports use on its next fetch, which the next request after the 30 s window triggers.

## Configuration (settings page / `awm config`)

| Field | Default | Purpose |
|---|---|---|
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

- `edge_material` is loopback-only: it refuses any call that carries an
  identity stamp, and the loopback gateway is never exposed off-host.
- The signing secret is minted once and never rotated. Rotating it drops every
  live session.
