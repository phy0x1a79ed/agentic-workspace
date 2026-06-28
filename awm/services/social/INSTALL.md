# Installing the `social` service

A Python feature service in the `awm.social` namespace. It connects to external
messaging platforms (Discord, Slack today; Teams / Gmail / WeChat later) and
exposes one unified `social_*` MCP surface for sending, receiving, and operator
management. It needs the `awm` conda env to contain its package plus the shared
component libraries it imports (`config`, `persistence`, `gatewayclient`) and its
platform SDKs (`discord.py`, `slack_sdk`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Accounts (`<AWM_DIR>/social.toml`)

The service acts as one or more **named accounts**. Each is a `[account.<name>]`
section naming a `platform` + its token(s). Create the file mode **0600** (it
holds secrets) — the service still boots with **zero live connections** if it is
absent. Tokens live ONLY here; the service DB and logs hold metadata only, never
the token.

```toml
[account.discord-bot]          # Discord is bot-only (a user token is a self-bot
platform = "discord"           # and violates Discord ToS — not supported).
token = "..."                  # Bot token from the Discord developer portal.

[account.slack-bot]
platform = "slack"
token = "xoxb-..."             # Bot OAuth token (send + list channels).
app_token = "xapp-..."         # App-level token; REQUIRED for Socket Mode receive.

[account.slack-me]             # Legitimate "me" identity: a Slack user OAuth token.
platform = "slack"
token = "xoxp-..."
app_token = "xapp-..."

[account.gmail-me]             # Gmail via a Google App Password (SMTP + IMAP).
platform = "gmail"
address = "you@gmail.com"      # the mailbox; also the SMTP/IMAP login user.
token = "abcd efgh ijkl mnop"  # 16-char App Password (NOT your normal password).
```

You create the Discord bot / Slack app yourself and paste the tokens here; no
OAuth-callback server is built.

**Gmail** uses a Google **App Password** (no OAuth client / callback server):
enable 2-Step Verification on the Google account, then create an App Password at
<https://myaccount.google.com/apppasswords> and paste the 16-char value as
`token` with the mailbox as `address`. The connector sends over `smtp.gmail.com`
and receives by polling `imap.gmail.com` (reading with `BODY.PEEK`, so it never
marks your mail read). For `social_send`, `channel` is the recipient address and
`thread` (optional) is an RFC822 `Message-ID` to reply into; a `Subject:` first
line in the text sets the subject. UBC-webmail / WeChat later get their own path.

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/social`; it execs this same `run.sh` as an overlay.
