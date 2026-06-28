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
| `awm-slack` | Slack desktop (snap), CDP enabled | 9223 |
| `awm-opera-teams` | Opera (snap): Teams web + Slack-web | 9224 |
| `awm-vnc` | `x11vnc` for the one-time login (**on-demand**, not enabled) | 5920 |

All GUI units force the X11 Ozone backend (`--ozone-platform=x11`) and unset
`WAYLAND_DISPLAY` — otherwise Chromium/Electron auto-detect the host's wayland
socket and render to the real GNOME session instead of `:20`.

`awm-slack-creds` (in `~/.local/bin`) attaches to a CDP port and prints
`{"token","cookie","team","url"}` — the live `xoxc-` session token + `d` cookie.
The awm host calls it over ssh as the connector's `creds_cmd`; no secret hits disk.

## Install / update

From the awm host (where this repo lives):

    rsync -a awm/services/social/mira/ mira:awm-social-mira/
    ssh mira 'bash ~/awm-social-mira/install-mira.sh'

One-time prereqs (already done on this mira): `sudo snap install slack opera`,
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
    # openbox has no taskbar: Alt+Tab cycles windows, right-click desktop = menu.
    ssh mira 'systemctl --user stop awm-vnc'     # when done

Recommended path is the **Opera tabs** (both render on `:20` and are VNC-visible).
The Slack *desktop* app is installed and on `:9223` too, but its headless OAuth
deep-link is fiddlier. The extractor tries the desktop (`:9223`) first, then falls
back to the app.slack.com tab in Opera (`:9224`) automatically — so logging in to
just the Opera Slack tab is enough.

## Verify

    ssh mira ~/.local/bin/awm-slack-creds         # tries desktop, then Opera

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
- Driving Slack with a web-session token is against Slack's automation ToS; this is
  the user's own account/session at their explicit request (see service INSTALL.md).
