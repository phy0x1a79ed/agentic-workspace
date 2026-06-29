# Installing the `social` service

A Python feature service in the `awm.social` namespace. It connects to external
messaging platforms (Discord, Slack today; Teams / Gmail / WeChat later) and
exposes one unified `social` MCP domain for sending, receiving, and operator
management — agents call `social(verb="send", args={…})` etc. (run
`social(verb="describe")` to list verbs); the operations stay expanded as
`social_*` on the CLI/HTTP surfaces (`awm social send`, `{name:"social_send"}`). It needs the `awm` conda env to contain its package plus the shared
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

[account.slack-mira]           # "me" via a durable web session on the mira host —
platform = "slack"             # no Slack app needed; creds fetched live, never on
creds_cmd = "ssh mira /home/tony/.local/bin/awm-slack-creds"  # disk. See below.

[account.gmail-me]             # Gmail via a Google App Password (SMTP + IMAP).
platform = "gmail"
address = "you@gmail.com"      # the mailbox; also the SMTP/IMAP login user.
token = "abcd efgh ijkl mnop"  # 16-char App Password (NOT your normal password).
```

You create the Discord bot / Slack app yourself and paste the tokens here; no
OAuth-callback server is built.

**Slack as "me" without an app (`creds_cmd` / mira).** A Slack web-session pair (an
`xoxc-` token + the `d` cookie) lets the service act as *you* with no app or bot —
but that pair dies when the browser session ends. The `mira/` helper keeps a Slack
client logged in permanently on the always-on **mira** host and exposes a one-line
extractor; `creds_cmd` pulls a fresh `{token, cookie}` from it at start and on any
auth failure, so nothing secret is stored. See `mira/README.md` for the host setup
(snap Slack/Opera under Xvfb, systemd user units, the one-time VNC login). You may
instead pin the pair inline (`token = "xoxc-..."` + `cookie = "xoxd-..."`, or
`cookie_file`), but it will expire. Driving Slack with a web-session token is against
Slack's automation ToS — this is your own account/session, at your explicit request.

**Gmail** uses a Google **App Password** (no OAuth client / callback server):
enable 2-Step Verification on the Google account, then create an App Password at
<https://myaccount.google.com/apppasswords> and paste the 16-char value as
`token` with the mailbox as `address`. The connector sends over `smtp.gmail.com`
and receives by polling `imap.gmail.com` (reading with `BODY.PEEK`, so it never
marks your mail read). For `social_send`, `channel` is the recipient address and
`thread` (optional) is an RFC822 `Message-ID` to reply into; a `Subject:` first
line in the text sets the subject. UBC-webmail / WeChat later get their own path.

## Reading history & DMs

Live receive only tails messages that arrive *after* connect. To reach existing
messages — including ones from before the service came up — and DMs:

- **`social_history account= channel= [limit=] [before=]`** fetches a channel's
  existing messages via its connector, persists them through the same dedupe path
  as live inbound, and returns them. They then show up in `social_messages` and
  `social_search`. `before` pages backwards (Slack ts / connector cursor; Teams
  has no usable cursor and ignores it).
- **`social_channels account= [include_dms=true]`** lists channels; with
  `include_dms` it also enumerates direct/group DMs (`kind` `dm`/`group`) where
  the platform supports it (Slack `im`/`mpim`, Teams 1:1/group chats).
- **`social_open_dm account= user=`** resolves a platform user — by id, or by
  name where the platform supports a directory lookup (Slack `users.list`) — to a
  DM channel and opens it, returning the channel id for use with `history`/`send`.
- **`social_backfill account= [limit=] [include_dms=true]`** enumerates *every*
  conversation the account can see (channels + DMs) and runs `history` over each,
  so `social_search` covers the account's full reachable history. Conversations a
  connector genuinely can't read (e.g. Teams `@thread.tacv2` team-channels, served
  by a different backend than the ng.msg chat service) are reported per-row with
  `ok: false` + the error, never silently dropped.

Slack and Teams run through the mira daemon; the DM/conversation enumeration and
`open_dm` for those platforms live in `mira/mira_api/` and require the mira host
to be running the updated daemon (`mira/install-mira.sh`).

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
Under a shadow `AWM_WORKSPACE` is rewritten to a sandbox dir, so point the
service at a real `social.toml` with **`AWM_SOCIAL_CONFIG=/abs/path/social.toml`**
(the parallel of the 2fa service's `AWM_2FA_DIR`).

## Discord slash command (`/approve`)

The Discord connector registers one application command, `/approve [device]`,
usable only as a **DM to the bot** (`interaction.guild is None`; guild-channel
invocations are rejected). It's synced as a *global* command in `on_ready` —
global is the only kind that appears in a DM. On invocation the connector acks
within Discord's 3s deadline and emits a normalised event on the `command`
emitter topic (`/svc/social/emit/command`) with `{command, device, account,
channel_id, …}`. The service does nothing with `approve` itself — the **`2fa`**
service subscribes to that topic and arms a Duo approval burst (see its INSTALL).

Prereqs: a `[account.*]` with `platform = "discord"` and a bot token whose app
was invited with the `applications.commands` scope. New slash commands can take
up to ~1h to propagate on first global sync (usually faster).
