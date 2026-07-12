# Installing the `fileviewer` service

A Python feature service in the `awm.fileviewer` namespace. It exposes files on
the gateway origin as an **origin-relative** URL:

    /files/home/tony/foo.svg          →  https://<host>:12100/files/home/tony/foo.svg

PNG/JPEG display as images, SVG draws, HTML renders, `.py`/`.md`/`.json` show
inline — each served by the gateway with a `Content-Type` from `mimetypes`, so
the browser picks the right renderer. A path that doesn't exist, resolves outside
the mount root, or is **masked** (see below) returns a plain `404`.

## How it works — a masked gateway static mount

fileviewer does **not** run its own HTTP listener. Instead it registers a
`kind=static` **mount** at the `/files` prefix on the gateway (root
`FILEVIEWER_MOUNT_ROOT`, default `/`) and holds that mount's WS lease for the life
of the process. The gateway's `serve_static` ships the bytes and `httpsfront`
fronts the whole gateway — so a `/files/<abs-path>` link is:

- **origin-relative** — no host, no port, no `?path=` query. It resolves against
  whatever host is serving the page, so it renders on **any** device that can
  reach the gateway (a phone, another laptop), not just the server's own
  loopback. (The old loopback-only `http://127.0.0.1:12210/?path=…` listener is
  retired — it was never reachable through the HTTPS front.)
- **served the standard awm way** — no bespoke side-channel, no httpsfront route.

Two registrations, one process: the `ServiceAdapter` control WS (`kind=service`
at `/svc/fileviewer`) buys **supervision + the `fileviewer_status` verb**; the
separate `kind=static` mount is what serves the bytes and runs its own
register/hold-lease/reconnect loop (records are in-memory, so it must survive a
gateway restart or the mount vanishes).

### The mask (hiding secrets)

Because the mount exposes the whole filesystem under the root, a **denylist**
hides secrets. It is a per-mount `deny` glob list enforced gateway-side in
`serve_static`, matched with `PurePosixPath.full_match` (so `**` spans
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

The mount-holder uses `httpx` + `websockets` (already deps of the adapter) to
register and hold the lease. There is no database, so no `awm-persistence`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `FILEVIEWER_MOUNT_PREFIX` | `/files` | origin path the mount claims |
| `FILEVIEWER_MOUNT_ROOT` | `/` | filesystem root the mount exposes |
| `FILEVIEWER_MASK_FILE` | *(none)* | extra deny globs (gitignore-style); self-hidden |

## Scope & caveats

- **All files under the root are exposed** except the mask — by design (the mount
  is for viewing arbitrary figures/files). It rides the HTTPS front, so unlike
  the old loopback listener it is reachable on the LAN/ZeroTier SAN IPs. The mask
  is the safety layer; keep it current, or narrow the root.
- **Directory URLs** serve an `index.html` if present, else 404 — there is no
  directory listing (the old single-file viewer had none either).
- **Content-type** comes from `mimetypes` (extension-based). Unknown code
  extensions may download rather than render inline — irrelevant for the PNG
  figures this serves.

## Verify

    awm services list                       # fileviewer → running
    awm fileviewer status                   # mounted/prefix/root/deny_globs
    # a real file through the front:
    curl -sk -o /dev/null -w '%{content_type} %{http_code}\n' \
      "https://127.0.0.1:12100/files/<abs-path>.png"          # image/png 200
    # a masked secret:
    curl -sk -o /dev/null -w '%{http_code}\n' \
      "https://127.0.0.1:12100/files/home/tony/.ssh/id_ed25519" # 404

Then open a figure URL in a real browser (ideally from another device) to confirm
it *renders*, not just transfers.
