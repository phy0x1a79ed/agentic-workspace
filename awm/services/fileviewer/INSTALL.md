# Installing the `fileviewer` service

A Python feature service in the `awm.fileviewer` namespace. It exposes files on
the gateway origin as an **origin-relative** URL:

    /s/fileviewer/home/tony/foo.svg   →  https://<host>:12100/s/fileviewer/home/tony/foo.svg

PNG/JPEG display as images, SVG draws, HTML renders, `.py`/`.md`/`.json` show
inline — each served with a `Content-Type` from `mimetypes`, so the browser picks
the right renderer. A path that doesn't exist, resolves outside the root, or is
**masked** (see below) returns a plain `404`.

## How it works — a masked service mount

fileviewer serves files on a loopback HTTP listener and registers that URL as
its service `mount`. The gateway proxies `/s/fileviewer/<rest>` to it and
`httpsfront` fronts the gateway, so a link resolves against whatever host serves
the page and renders on any device that can reach the gateway. Peers do not
reach the listener: the edge carries only tool calls.

### The mask (hiding secrets)

Because the mount exposes the whole filesystem under the root, a **denylist**
hides secrets. It is a `deny` glob list the listener enforces, matched whole (so `**` spans
directories) against the **symlink-resolved** path — a symlink to a secret can't
slip past. A masked path 404s exactly like a missing one. The default mask covers
ssh/gpg keys, `*.pem`/`*.key`/`*.p12`, `.certs`, tokens (`auth.token`, `*.token`,
`.aws`, `.netrc`, `credentials`), `.env*`, and `.git`. Extend it per-host with
`FILEVIEWER_MASK_FILE` (gitignore-style, one glob per line); that file is added
to the mask so it **hides itself**.

The mask is best-effort and denylist-shaped: a new secret type in an unlisted
location is exposed until added. Root `/` maximizes what a mask gap can leak;
narrow `FILEVIEWER_MOUNT_ROOT` to bound the blast radius.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`) and writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` = the env's absolute interpreter, so the gateway can respawn the
service under a minimal PATH (where `mamba` is not present).

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control loop) |

The listener is stdlib `http.server`. There is no database, so no
`awm-persistence`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `FILEVIEWER_MOUNT_ROOT` | `/` | filesystem root the mount exposes |
| `FILEVIEWER_PORT` | ephemeral | loopback port of the listener |
| `FILEVIEWER_MASK_FILE` | *(none)* | extra deny globs (gitignore-style); self-hidden |

## Scope & caveats

- **All files under the root are exposed** except the mask — by design (the mount
  is for viewing arbitrary figures/files). It rides the HTTPS front, so it is
  reachable on the LAN/ZeroTier SAN IPs. The mask
  is the safety layer; keep it current, or narrow the root.
- **Directory URLs** serve an `index.html` if present, else 404 — there is no
  directory listing.
- **Content-type** comes from `mimetypes` (extension-based). Unknown code
  extensions may download rather than render inline — irrelevant for the PNG
  figures this serves.

## Verify

    awm services list                       # fileviewer → ready
    awm fileviewer status                   # mounted/prefix/root/listen/deny_globs
    # a real file through the front:
    curl -sk -o /dev/null -w '%{content_type} %{http_code}\n' \
      "https://127.0.0.1:12100/s/fileviewer/<abs-path>.png"          # image/png 200
    # a masked secret:
    curl -sk -o /dev/null -w '%{http_code}\n' \
      "https://127.0.0.1:12100/s/fileviewer/home/tony/.ssh/id_ed25519" # 404

Then open a figure URL in a real browser (ideally from another device) to confirm
it *renders*, not just transfers.
