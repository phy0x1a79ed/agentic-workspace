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
after each reconnect, every 10 minutes, and on first sight of a chat. Only the
sync moves a chat's cursor, so a live event that lands first never hides a gap.

**CAUTION** The sync re-reads replies only on threads with activity in the 7
days below the cursor. A reply that revives an older thread while the socket is
down stays out of history.

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

## Reactions

`social_react` puts an emoji on a message, or takes it off with `remove`.
`emoji` is a character, a Slack name or `:name:`. The names live in
`awm/social/emoji.py`.

- **Discord.** The bot reacts. A Discord user token is a self-bot under
  Discord's terms, so no account reacts as Tony there.
- **Slack.** The Opal app lacks `reactions:write` until a Workspace Owner
  reinstalls it from `slack-bot-manifest.json`. Until then every Slack account
  posts the emoji alone as a message, into `thread` when given, and answers
  `"as": "message"`. `remove` refuses, because there is no reaction to take off.
- **Gmail and Teams.** Refuse.

## Files

`social_send` attaches a file with `file`, a path on the calling node, and
inline text files with `files`, each `{filename, text}`. The gateway's file hop
carries `file` to the node that runs social. A `send` that a borrower forwards to
the owner carries the file inline as base64.

- **Discord.** The bot attaches up to 10 MB per message.
- **Slack.** Every Slack account uploads through Slack's external upload, as a
  plain file and never a canvas. The reply carries the file ids and permalinks
  and no message ts, because Slack shares the file after the call returns. The
  Opal app needs `files:write`, so its uploads refuse until a Workspace Owner
  reinstalls it from `slack-bot-manifest.json`. Tony's Slack runs through the
  mira daemon, which takes 8 MB per message.
- **Gmail and Teams.** Refuse. Teams takes files only through SharePoint.

## Seats

A seat is one channel or chat a bot account is in. `social_seats` lists them
with their holders. A Claude Code background session mounts a seat with
`social_mount`. Each later message in that seat, from anyone but the bot, is
typed into the holder's prompt through `reflection message`, one line each:

    slack from <sender>[ [bot]][ ↳<thread ts>] [channel=<id> message_id=<id>]: <text>

` [bot]` marks a bot sender, so an agent can tell another swarm's bot from a
person. The ids are the `channel` and `message_id` arguments of `social_react`
and `social_send`. They tell a session that holds several seats which chat a
line came from. Newlines become ` ⏎ `. Text past 1500 characters is cut, and attachments add
`[+N files]`. A mount replays nothing, so read the backlog with
`social_history`.

- **One holder.** A mount on a held seat is refused and names the holder.
  `force=true` takes it, and the old holder gets one line saying it lost the
  seat. The swarm is cooperative, so force is the recovery path.
- **Release.** `social_unmount` frees one seat, or every seat the caller holds.
  The owner prunes every 60 s and frees each seat whose job is gone or stopped.
  An idle background session retires after about an hour, which frees its seats
  the same way. A node that does not answer keeps its seats.
- **Delivery.** Each holder has one ordered queue. A refused line (the holder
  sits at a dialog) is retried with backoff for 2 minutes, then dropped.
  History still holds it.
- **Discord DM.** A Discord DM is the operator's notification channel and is
  never a seat. `social_seats` lists guild text channels only.
- **Name.** A holder goes by its seat: `seat:<app>/<server>/<channel>`, or
  `seat:<app>/DM/<person>` for a DM, such as `seat:discord/Hallam Hive/general`
  or `seat:slack/arbutus`. A kept seat's agent starts with that name. The mount
  reply's `rename` tells a session that holds only that seat to take the name.
- **Acknowledge.** The mount reply and the kept-seat brief tell a holder to
  react 👀 on the message before work that takes a while, then answer with
  `social_send`. A reaction marks the request as seen without a message that
  says only "on it".

The holder is the calling session, never an argument. On the owner node the
gateway stamps `_caller_pid`. A caller on another node arrives through the
owner's edge, and its MCP proxy adds `_caller_session` from its own session
record.

**CAUTION** Two lists must agree for a cross-node mount:
`SESSION_CLAIM_VERBS` in `awm/gateway/awm/gateway/mcp_caller.py` and the
`social` entry of `_CALLER_STAMPED` in `server.py` beside it. The owner strips
`_caller_session` from any call that skips the edge. A proxy started before the deploy sends no
claim, so restart the session after you deploy.

### Kept seats

A kept seat is a seat whose holder social starts and keeps alive. Declare it
in the owner's `social.toml`:

    [seat.hallam-general]
    account = "discord-bot"
    channel = "1560400515630760049"    # the chat id, as social_seats lists it
    node    = "altair"                 # where the agent runs: it needs Claude Code
    scope   = "awm/seat-hallam-general"
    brief   = """…"""                  # the agent's standing instructions
    # model = "opus"   effort = "high"  (optional)
    # compact_at = 200000              (optional: 200k to 960k tokens)

The seat stays an interface: the agent hears the chat only through pushed
lines and answers with `social_send`. It runs no Monitor. Social reads the
tables once, at start, so restart social after you edit one. A bad table is
logged and disables the kept seats only. The accounts keep running.

The brief and the chat rules go into the agent's system prompt, not its first
message. A compaction summary rewrites the conversation and never the system
prompt, so the brief survives every compaction word for word. The agent
compacts in place once its context reaches `compact_at`.

The keeper runs on the owner every 60 s:

1. It lists `cx` sessions on `node`, locally or through a domestic peer. A
   node that does not answer skips the tick.
2. It starts a missing agent with `cx start`, named for its chat, pinned,
   with Remote Control on. It writes the mount row itself.
3. It resumes an agent that exited, with `cx resume`. The agent keeps its own
   conversation and its prompt cache. The keeper then pushes it one line that
   names `social history since=<ts>`, the last tick that saw it alive, because
   lines pushed while it was down were dropped.
4. It starts a new agent when the old one failed on an API error, when
   `cx resume` refuses, or after three resumes that did not last 10 min.
   Restarts back off from 60 s to 15 min, and the count resets after 10 min
   of health.
5. It leaves an agent blocked on login or usage for a person, and logs it once.
6. It replaces an agent whose launch spec changed, once its tempo is idle.
   A recycle is not a restart.

**CAUTION** The spec hash leaves out the session name, so a renamed chat never
recycles a live agent. An agent keeps the name it started with, and an agent
started before seats were named for their chat keeps `seat - <name>` until its
next replacement.

**CAUTION** `compact_at` is where compaction fires, not the window. Claude Code
compacts 33k tokens below its `--autocompact` window, so the keeper passes
`compact_at` plus `AUTOCOMPACT_RESERVE` in `keeper.py`. Check that constant
when Claude Code changes its compaction rule.

A `cx stop` of a pinned agent unpins it and removes it with `claude rm`, so the
keeper calls it only before a new start. The Claude Code daemon still retires a
pinned session under memory pressure, and the next tick resumes it.
`social_mount` on a kept seat is refused and names the agent. `social_seats`
shows each kept seat's job, health, restart count and resume count.

**CAUTION** A new agent opens on the same brief and reads the backlog first.
Write the brief so that a second reading of a handled backlog posts nothing. A
brief that says "announce yourself on start" posts again on every new start.

**CAUTION** `cx start` refuses a scope worktree that Claude Code does not
trust. A scope in a repository that has never run Claude Code is refused on
every tick. Open one session there by hand first.

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
only kind that appears in a DM. The command tree allows the bot DM context only,
so Discord hides the commands in servers and group DMs. On invocation the connector checks the sender,
acks within Discord's 3s deadline and emits a normalised event on the `command`
emitter topic (`/svc/social/emit/command`) with `{command, account,
channel_id, operator, …}`. The service does nothing with a command itself.

The `/new-agent` ack is public in the DM and carries an `ack_ref` in its event.
A `send` with that `ack_ref` overwrites the ack with the answer. Discord allows
the edit for 15 minutes, so after 14 the `send` posts a new message instead.
Every message into a DM goes out with link previews suppressed.
A `send` with an `ack_ref` attaches its files to the edited ack.

A guild invocation that slips past the hidden context, or a sender absent from
the operators table, gets an ephemeral refusal and emits nothing. Add an operator on the node that owns the
bot account with `social add_operator platform=discord
platform_user_id=<discord id> awm_user=<name>`.

**WARNING** Add the operators before you deploy this gate. Until then every
command, `/approve` included, is refused.

Prereqs: a `[account.*]` with `platform = "discord"` and a bot token whose app
was invited with the `applications.commands` scope. New slash commands can take
up to ~1h to propagate on first global sync (usually faster).
