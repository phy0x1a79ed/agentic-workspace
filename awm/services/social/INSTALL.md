# Installing the `social` service

A Python feature service in the `awm.social` namespace. It connects to external
messaging platforms (Discord, Slack today; Teams / Gmail / WeChat later) and
exposes one unified `social` MCP domain for sending, receiving, and operator
management — agents call `social(verb="send", args={…})` etc. (run
`social(verb="describe")` to list verbs); the operations stay expanded as
`social_*` on the CLI/HTTP surfaces (`awm social send`, `{name:"social_send"}`). It needs the `awm` conda env to contain its package plus the shared
component libraries it imports (`config`, `persistence`, `gatewayclient`) and its
platform SDKs (`discord.py`, `slack_sdk`, `aiohttp`).

## Purpose & Contents

This file is the service's contract for whoever installs or configures it: the
install, the `social.toml` sections, and the behaviour a caller cannot read off
`social(verb="describe")`. Verbs and their arguments live in the manifest in
`awm/social/hub_adapter.py`, not here.

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

[account.slack-bot]            # A Slack app's bot user. An xoxb- token selects the
platform = "slack"             # plain-HTTPS bot connector (see "Slack bot" below).
token_file = "social-secrets/slack-opal.bot"     # Bot OAuth token (xoxb-).
app_token_file = "social-secrets/slack-opal.app" # App-level token (xapp-) for receive.
singleton = true

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

**Slack bot.** An `xoxb-` account runs `SlackBotConnector`: Web API calls over
aiohttp, receive over Socket Mode, no `slack_sdk` and no browser. To create one in
any workspace:

1. Create an app at <https://api.slack.com/apps> → *From a manifest*, pasting
   `slack-bot-manifest.json`.
2. Generate an app-level token with the `connections:write` scope. Save it as the
   `app_token_file`.
3. Install the app to the workspace. Copy the *Bot User OAuth Token* into the
   `token_file`.

**CAUTION** A workspace that restricts apps turns step 3 into an install request.
Only Workspace Owners and app managers receive it, not every admin. **CAUTION**
Mark the account `singleton = true`. Slack splits events across all open Socket
Mode connections, so a second node connecting the same app silently halves what
each one receives.

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

## Reading messages, searching & attachments

The external platforms are the source of truth, and every read below queries
them live. Live receive *emits* each message arriving after connect on the
`message` topic. Follow it with `awm services listen social message --peer
<owner>`, one JSON line per message.

A bot account (Slack `xoxb-`) is the one exception: it keeps a synced history of
every chat the bot is in, edits and deletes applied. It delta-syncs at start,
after each reconnect, every 10 minutes, and on first sight of a chat.

- **`social_history account= [channel=] [since=] [limit=]`** reads that store.
  With `since` (a message ts) it returns the messages after it, oldest first.
  Use it to catch up after `listen` prints `listen.reconnected`.

- **`social_fetch account= channel= [limit=] [before=]`** fetches a channel's
  existing messages live from the platform (including ones from before the
  service started), each with an `attachments` list (`idx`, `filename`, `mime`,
  `size`, `url`). `before` pages backwards (Slack ts / connector cursor; Teams
  has no usable cursor and ignores it). Nothing is stored.
- **`social_search account= query= [channel=] [limit=]`** runs the platform's
  **own** search live and returns matches with attachment metadata. Per platform:
  Slack uses `search.messages` (needs a user token with `search:read` — the
  session `xoxc` token has it; a `xoxb` bot token does not and surfaces a clean
  error); Gmail uses IMAP `X-GM-RAW` (full Gmail query syntax: `from:`,
  `has:attachment`, …) over *All Mail*; Teams is best-effort (no native chat
  search, so a client-side scan of recent messages per conversation); a Discord
  **bot** account cannot search at all — use `social_fetch`.
- **`social_download_attachments account= channel= message_id= [idx=]`** pulls a
  message's attachments to a **system temp dir outside the awm workspace**
  (honoring `$TMPDIR`) and returns
  `{files:[{filename, mime, size, path, url}], dir, node}`. It re-fetches the
  message live so signed/expiring urls are always fresh (Discord CDN links, Slack
  `url_private`). `idx` optionally selects one attachment. Returns paths, not
  bytes — large files never bloat the RPC payload (the verb declares a generous
  300s timeout for the download hop).
- **`social_channels account= [include_dms=true]`** lists channels; with
  `include_dms` it also enumerates direct/group DMs (`kind` `dm`/`group`) where
  the platform supports it (Slack `im`/`mpim`, Teams 1:1/group chats).
- **`social_open_dm account= user=`** resolves a platform user — by id, or by
  name where the platform supports a directory lookup (Slack `users.list`) — to a
  DM channel and opens it, returning the channel id for use with `fetch`/`send`.
  Discord takes a user id only.

On Discord, `channel` also accepts `dm:<user_id>`: `send` and `fetch` open that
user's DM with the bot on first use. Fleet notifications (ssh lock alerts, the
auth password push) target the operator this way, so they need no stored DM
channel id. The bot reaches a user only while they share a guild.

**The retrieval contract, when the call is borrowed.** `path` and `dir` are
absolute on the node that ran the download — which for a borrowed `social` is not
the caller's. That is what `url` is for: the same bytes' address on the serving
node's `fileviewer` mount, which the MCP proxy uses to pull them down and rewrite
`path` to a local copy (FEDERATION.md § *Cross-peer bytes*). Two consequences
worth knowing. A file the mount's denylist hides — `*.pem`, `*.key`, `*.token`,
`credentials`, anything under `secrets/` — cannot cross, and comes back as
`path: null` with a named `error` rather than a path that does not exist. And a
caller reaching the service by any other route (CLI, raw `/invoke`, a service
calling `social` directly) gets the serving node's paths verbatim and must use
`url` itself.

Slack and Teams run through the mira daemon; conversation enumeration, `open_dm`,
`search`, and attachment `download` for those platforms live in `mira/mira_api/`
and require the mira host to be running the updated daemon
(`mira/install-mira.sh`). Teams attachment download is best-effort: hosted-content
images resolve in-session, but SharePoint-hosted files may need separate auth and
are skipped rather than erroring the whole call.

## Cloud-drive buckets (Google Drive, OneDrive/SharePoint)

Beyond messaging, the service gives awm full **file** access to cloud drives
through a separate **buckets** surface (it is not a messaging connector). Each
`[bucket.<name>]` section in `social.toml` configures one store. The verbs are
the `bucket_*` entries of `social(verb="describe")`. Each verb's description
says which bucket kinds support it: a OneDrive bucket supports every verb, and
a Drive bucket lacks sites, sharing, versions and the recycle bin.

Paths are POSIX-style and **relative to the bucket's `root`** (omit `path`, or
pass `""`, for the root). A OneDrive bucket also takes an absolute
server-relative path (`/teams/<x>/...`, `/sites/<x>/...`,
`/personal/<user>/...`), so one bucket reaches every site the mira session can.
SharePoint's own permissions are the only boundary, and a refusal comes back as
a permission error. Entries under the root come back relative, entries
elsewhere absolute. `bucket_sites` finds the absolute paths.

File arguments and results are paths **on the calling node**, whichever node
runs social. `bucket_put` reads a local file or folder as `src`, and
`bucket_get` and `download_attachments` return local paths. The gateway
carries the bytes across the peer hop inside the one call (see `FEDERATION.md`
§ *Cross-peer calls*). Bytes never inline into the RPC, and downloads land in a
system temp dir outside the workspace.

**CAUTION** `bucket_share` is outward-facing. Its description tells an agent to
get Tony's confirmation of the item, the audience and the role first.
`send_email` stays false unless asked for.

**CAUTION** `overwrite` on `bucket_mv` and `bucket_cp` first sends the existing
target to the recycle bin (Drive: trash). `bucket_restore` brings it back.

```toml
[bucket.gdrive-phyber]          # Google Drive via an OAuth2 refresh token.
kind = "google_drive"
client_id = "....apps.googleusercontent.com"
client_secret_file = "gdrive.secret"   # or inline client_secret = "..."
refresh_token_file = "gdrive-phyber.token"   # or inline refresh_token = "..."
# root = "<drive-folder-id>"    # optional: scope to one Drive folder (default: whole drive)

[bucket.onedrive-ubc]           # UBC SharePoint, via the mira session (see below).
kind = "onedrive"
source = "mira"                 # OneDrive is reachable ONLY through the mira daemon
site = "https://ubcca.sharepoint.com"
root = "/teams/ubcMICB-gr-HallamLab/Shared Documents/Hallam_Lab_roadmaps/Tony-L_-roadmap-_"
```

**Google Drive (OAuth2).** A Google App Password (the Gmail path) cannot reach
Drive, so a bucket needs its own OAuth2 client. One-time, in the Google Cloud
console: create a project, enable the **Drive API**, configure the OAuth consent
screen, and create a **Desktop app** OAuth client. **Publish the consent screen
to Production.** Left in *Testing* it works for exactly seven days and then every
call fails with `invalid_grant: Bad Request` — a bucket that worked and silently
stopped a week later is this and nothing else, and re-minting the token without
publishing just buys another week. Then run the consent helper **once per
account**:

    python scripts/gdrive_auth.py --client-secrets /path/to/client_secret.json --bucket-name gdrive-phyber

Consent needs a browser and the redirect comes back to a local port, so on a
headless host forward one first (`ssh -L 8765:localhost:8765 <host>`) and add
`--port 8765 --no-browser`. Re-minting for a client already in `social.toml`
takes `--client-id` + `--client-secret-file` instead of the downloaded JSON.

It prints a `refresh_token` (and a ready TOML snippet). Paste the
`client_id`/`client_secret`/`refresh_token` into the section above (secrets
prefer `*_file` references to a gitignored file). The scope is the **full
`drive` scope** — read, overwrite, *and delete* across the whole account — so
treat the refresh token like a password. `social_bucket_search` matches file
names across the whole drive (Drive query can't scope a name search to a
sub-tree); Google-native docs are exported on `get` (Docs→pdf, Sheets→csv).

**OneDrive / SharePoint (via mira).** The UBC student account can't mint an
offline OAuth token without a Microsoft app registration + tenant admin consent,
so this reuses the **mira** pattern (the same host that drives Teams/Slack): the
mira daemon keeps logged-in SharePoint tabs in Opera and drives the SharePoint
REST API in-page over CDP, same-origin with the session cookie. The bucket here
is a thin client of the daemon's `/v1/fs/onedrive/…` routes. It requires the
`[mira]` block (already configured for Teams/Slack) and the daemon with
`MIRA_STORAGE=onedrive` plus logged-in `<tenant>.sharepoint.com` and
`<tenant>-my.sharepoint.com` tabs. The second tab serves personal OneDrive. See
`mira/README.md`. The `root` is a **server-relative path** (decode `%20` etc.
from the share URL, or pass the link to `bucket_resolve`).

The Google libraries (`google-api-python-client`, `google-auth`,
`google-auth-oauthlib`) are declared deps, imported lazily — the service still
boots without them; only `google_drive` buckets need them.

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

## Discord slash commands

The Discord connector registers three application commands. Each works only as
a **DM to the bot**, and only for a sender in the `social_operators` table.

| Command | Consumer |
|---|---|
| `/approve [device]` | `2fa` and `ssh` arm a Duo approval burst (see their INSTALL) |
| `/reject <ref> [reason]` | `reporting` rejects the federation card |
| `/new-agent [prompt]` | `reporting` starts a session with Remote Control on and answers with its link |

The commands sync as *global* commands in `on_ready`, because global is the
only kind that appears in a DM. On invocation the connector checks the sender,
acks within Discord's 3s deadline and emits a normalised event on the `command`
emitter topic (`/svc/social/emit/command`) with `{command, account,
channel_id, operator, …}`. The service does nothing with a command itself.

A guild invocation, or a sender absent from the operators table, gets an
ephemeral refusal and emits nothing. Add an operator on the node that owns the
bot account with `social add_operator platform=discord
platform_user_id=<discord id> awm_user=<name>`.

**WARNING** Add the operators before you deploy this gate. Until then every
command, `/approve` included, is refused.

Prereqs: a `[account.*]` with `platform = "discord"` and a bot token whose app
was invited with the `applications.commands` scope. New slash commands can take
up to ~1h to propagate on first global sync (usually faster).
