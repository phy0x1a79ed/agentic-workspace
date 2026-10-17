# mira — durable Slack/Teams session host for the `social` service

The social connector can't rely on a Slack token scraped from a laptop browser:
those die the moment the browser session ends. Instead a Slack client runs
**permanently on mira** (an always-on Linux host), and the connector reads a *live*
credential pair from it on demand. mira is the "backend of the backend" — there is
no UI here.

## What runs on mira

| systemd user unit | what | port (127.0.0.1) |
|---|---|---|
| `awm-display` | `Xvfb :20` virtual framebuffer | — |
| `awm-wm` | `openbox` — maps/focuses windows on `:20` | — |
| `awm-opera-teams` | Opera (snap): Teams web + Slack-web + SharePoint + OneDrive | 9224 |
| `awm-mira-api` | the API daemon — REST + WS, drives both Opera tabs over CDP | **172.16.0.24:7822** |
| `awm-vnc` | `x11vnc` for the one-time login (**on-demand**, not enabled) | 5920 |

All GUI units force the X11 Ozone backend (`--ozone-platform=x11`) and unset
`WAYLAND_DISPLAY` — otherwise Chromium/Electron auto-detect the host's wayland
socket and render to the real GNOME session instead of `:20`.

`awm-slack-creds` (in `~/.local/bin`) attaches to the Opera CDP port and prints
`{"token","cookie","team","url"}` — the live `xoxc-` session token + `d` cookie.
The awm host calls it over ssh as the connector's `creds_cmd`; no secret hits disk.
(This is the legacy Slack-only path; the API daemon below supersedes it and adds
Teams.)

## API daemon (`awm-mira-api`)

The formal service. An aiohttp daemon (`mira_api/`, deployed to
`~/.local/share/awm-social-mira/mira_api`) that drives **both** logged-in Opera
tabs over CDP and exposes one clean API over the awm-network. The awm `social`
service's Slack/Teams connectors are thin clients of it (`source = "mira"`).

It binds **`172.16.0.24:7822` only** (the awm-network interface — not
`0.0.0.0`/docker/wan), serves **TLS** from `~/.awm/tls/`, and requires
`Authorization: Bearer <~/.awm/auth.token>` on every request.

    GET  /v1/health                       per-platform CDP target liveness
    GET  /v1/{platform}/identity          who the session is logged in as
    GET  /v1/{platform}/channels          channels/conversations visible
    GET  /v1/{platform}/messages?channel=&limit=   recent history (+ attachments)
    GET  /v1/{platform}/search?query=&limit=&channel=   native message search
                                          (Teams: best-effort recent-message scan)
    GET  /v1/{platform}/download?channel=&message_id=&idx=   one message's
                                          attachments as [{filename,mime,b64}]
    POST /v1/{platform}/send  {channel,text,thread?}
    POST /v1/{platform}/upload  {channel,files:[{filename,b64}],text?,thread?}
                                          (Slack only; Teams answers 501)
    GET  /v1/events                       WS; pushes inbound {type:"message", …}

    # cloud file stores (OneDrive/SharePoint), gated by MIRA_STORAGE
    GET  /v1/fs/{name}/<op>?path=…        reads: ls, stat, get, sites, versions, …
    POST /v1/fs/{name}/<op>  {path, …}    writes: put, upload, mkdir, mv, share, …

The full `/v1/fs` route list and its error codes are in the module docstring of
`mira_api/server.py`. A file-store failure is a 404, 409, 403 or 400 with a
`code`, so the bucket raises the matching Python exception. A 502 means the
daemon could not classify the failure.

Bytes cross the hop in ranges, never whole. `get` returns one byte range per
call. `upload` takes one chunk per call, and the caller owns the upload id and
the offset, so the daemon keeps no upload state. **CAUTION** the daemon's
request cap (`MAX_REQUEST_BYTES`, 16 MiB) must stay above one base64-encoded
bucket chunk (`CHUNK` in `onedrive_bucket.py`, 8 MiB) and one base64-encoded
upload (`MAX_UPLOAD_BYTES` in `mira_conn.py`, 8 MiB). Lower both before you
lower the cap.

Messages carry an `attachments` array (`idx, filename, mime, size, url, ref`);
`/download` re-fetches the message in-session and returns the file bytes base64-
encoded over the REST hop (Slack `url_private` with the `d` cookie; Teams hosted
content — SharePoint-hosted files may need separate auth and are skipped).

`{platform}` is `slack` or `teams`; `{name}` (the `/v1/fs/…` routes) is a cloud
file store from `MIRA_STORAGE` (currently `onedrive`). The daemon polls each
*platform* on mira (the **inbound watcher**) and pushes new messages to all WS
clients, so awm-side clients never poll; file stores have **no** watcher (files
don't push) — their routes are request/response only. A conversation whose history endpoint 404s (e.g. a Teams
*team channel*, served by a different backend than the 1:1/group chat service)
is marked unreadable and skipped — it never stalls the poll.

### How the drivers work (in-page fetch, live session creds)

Every op runs JavaScript in the platform's Opera page, so requests ride the live
session's own cookies/tokens — the daemon reconstructs nothing on the wire.

* **Slack** — reads the `xoxc-` token from `localStorage['localConfig_v2']` and
  does a same-origin `fetch('/api/<method>')` on the `app.slack.com` tab (the `d`
  cookie rides automatically). `chat.postMessage`, `conversations.{list,history}`,
  `users.conversations`, `auth.test`.
* **Teams** — bootstraps `POST /api/authsvc/v1.0/authz` with the cached
  `api.spaces.skype.com` MSAL token → `{skypeToken, regionGtms.chatService}`
  (e.g. `https://ca.ng.msg.teams.microsoft.com`). All ops then hit
  `{chatService}/v1/users/ME/conversations…` with header
  `Authentication: skypetoken=<tok>` (re-bootstraps on 401/403). Graph is
  send-only here (`/me` works but chats need scopes the page's token lacks), so
  the ng.msg chat service is the substrate for list/read/send.
* **OneDrive / SharePoint** (`storage_drivers.py`) — same-origin calls to the
  SharePoint REST API with the session cookie. Same-origin fetch reaches only
  the tab's own host, so the driver holds two pages: the `ubcca.sharepoint.com`
  tab serves team and project sites, and the `ubcca-my.sharepoint.com` tab
  serves personal OneDrive (`/personal/...`). A missing `-my` tab fails only
  personal paths. Paths are absolute server-relative urls. The `_api` web base
  comes from the managed-path prefix (`/teams/<x>`, `/sites/<x>`,
  `/personal/<x>`), so one bucket reaches every site the session can.
  Behaviour the code alone does not show:
  - A missing folder answers 200 with `Exists: false`, never a 404, so `ls`
    and `stat` check `Exists` before they trust a listing.
  - A folder over the list view threshold (5000 items) refuses `/Files` and
    `/Folders`. `ls` then pages the library's `RenderListDataAsStream` view.
  - `mv` and `cp` use `SP.MoveCopyUtil` inside one site collection. Across site
    collections they run a copy job (`CreateCopyJobs`), which keeps version
    history and sharing but cannot rename, so the name stays the same.
  - `resolve` asks the v2.0 shares API first. Failing that, it reads the
    item's UniqueId from the link token: 16 bytes little-endian after a short
    prefix. The prefix was two bytes in the one decoded sample, so one byte is
    tried as well.

### Wire it into `social.toml`

    [mira]
    url = "https://172.16.0.24:7822"
    token_file = "~/.awm/auth.token"
    verify_tls = false                 # self-signed cert; pin it later to enable

    [account.slack-via-mira]
    platform = "slack"
    source = "mira"

    [account.teams]
    platform = "teams"
    source = "mira"

    [bucket.onedrive-ubc]              # OneDrive/SharePoint file access (buckets)
    kind = "onedrive"
    source = "mira"
    site = "https://ubcca.sharepoint.com"
    root = "/teams/ubcMICB-gr-HallamLab/Shared Documents/Hallam_Lab_roadmaps/Tony-L_-roadmap-_"

### Verify

    # health (from the awm host, over the awm-network)
    curl -sk -H "Authorization: Bearer $(ssh mira cat ~/.awm/auth.token)" \
      https://172.16.0.24:7822/v1/health

    # daemon unit tests (host tooling — standalone, not an awm dist)
    cd awm/services/social/mira && PYTHONPATH=. python -m pytest

## Install / update

From the awm host (where this repo lives):

    rsync -a awm/services/social/mira/ mira:awm-social-mira/
    ssh mira 'bash ~/awm-social-mira/install-mira.sh'

**CAUTION** `install-mira.sh` runs `enable --now`, which leaves a running unit
on its old code. Run `systemctl --user restart awm-mira-api` after a daemon
change. A new Opera start URL applies only at the next Opera restart, and that
restart also bounces the Slack and Teams tabs. To add a tab without it, run
`curl -X PUT 'http://127.0.0.1:9224/json/new?<url>'` on mira.

One-time prereqs (already done on this mira): `sudo snap install opera`,
`sudo apt-get install -y x11vnc openbox scrot python3.12-venv`,
`loginctl enable-linger tony`.

## One-time login (interactive, over VNC)

The clients start logged-out. Sign in once; the session then persists across
restarts/reboots (`~/snap/slack`, `~/snap/opera`).

    ssh mira 'systemctl --user start awm-vnc'
    ssh -N -L 5920:127.0.0.1:5920 mira          # keep open, from the laptop
    # point any VNC viewer at localhost:5920, then in the Opera window:
    #   - the "Microsoft Teams" tab → sign in (incl. 2FA)
    #   - an "app.slack.com" tab    → sign in to the workspace (incl. 2FA)
    #   - the "ubcca.sharepoint.com" and "ubcca-my.sharepoint.com" tabs → should
    #     SSO in from the Teams login; if one lands on a sign-in page, complete it
    #     once (OneDrive bucket: sites, and personal OneDrive)
    # openbox has no taskbar: Alt+Tab cycles windows, right-click desktop = menu.
    ssh mira 'systemctl --user stop awm-vnc'     # when done

**A SharePoint 403 usually needs no login at all.** `ls folders 403` from the
OneDrive driver reads like a lapsed sign-in, but the tab's `FedAuth`/`rtFa`
cookies expire long before the Teams login behind them does — and the page keeps
rendering from cache, so it *looks* signed in while every `_api` call returns
`UnauthorizedAccessException`. Navigating the tab to its own URL re-SSOs
silently. Drive it over CDP (`Page.navigate` on the `sharepoint.com` target at
`127.0.0.1:9224`), confirm with a fetch of
`<site>/_api/web/currentuser`, and only reach for VNC if that lands on a sign-in
page. The driver does not do this itself yet, so the 403 recurs.

Every session lives in **Opera tabs** — they render on `:20`, so they are all
VNC-visible, and one login per tab is all that is needed.

## Verify

    ssh mira ~/.local/bin/awm-slack-creds         # the app.slack.com tab, :9224

Should print one JSON line (`{"token","cookie","team","url"}`). Plug into
`social.toml`:

    [account.slack-me]
    platform = "slack"
    creds_cmd = "ssh mira /home/tony/.local/bin/awm-slack-creds"

## Notes / guardrails

- All debug + VNC ports bind `127.0.0.1` only. The extractor runs *on* mira; the
  CDP port never leaves the host. VNC is reachable solely through an ssh tunnel.
- Do **not** disturb `~/.config/virtual-auth/` or `app-virtual-auth.slice` — that's
  the live 2FA device, unrelated to this.
- The user's own `google-chrome` profile is untouched; Teams uses a separate Opera.
- Driving Slack/Teams with a web session is against those platforms' automation
  ToS; this is the user's own account/session at their explicit request (see
  service INSTALL.md).
- The API daemon binds the awm-network interface only and is TLS + bearer-token
  gated, but it can send/read as the user's Slack+Teams **and read/write/delete
  files in the logged-in SharePoint/OneDrive** (`MIRA_STORAGE`) — treat the
  bearer token in `~/.awm/auth.token` as the key to all of those accounts.
