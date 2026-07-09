# Installing the `fileviewer` service

A Python feature service in the `awm.fileviewer` namespace. Point a URL at any
file's absolute path and the browser renders it natively:

    http://127.0.0.1:12210/?path=/home/tony/foo.svg

SVG draws, HTML renders, PNG/JPEG display as images, `.py`/`.md`/`.json`/`.log`
show inline as readable text — each served with a correct `Content-Type` so the
browser picks the right renderer. A path that doesn't exist, is a directory, or
can't be read returns an HTTP 404 with a small styled not-found page (not a
stack trace, not a JSON blob).

Unlike most services, the file bytes do **not** ride the awm hub: the hub
function channel is JSON-only (a handler's return value is always
JSON-serialized), so it can't hand a browser raw bytes with a real
`Content-Type`. So the listener runs its own loopback HTTP server on a fixed
port (default **12210**), and the gateway registration provides supervision + a
`fileviewer_status` surface only — exactly the `mic` pattern.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`) and writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` = the env's absolute interpreter, so the gateway can respawn the
service under systemd's minimal PATH (where `mamba` is not present).

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control loop) |

The listener itself is **pure stdlib** — `http.server`, `mimetypes`, `pathlib`.
There is no database, so no `awm-persistence`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `FILEVIEWER_PORT` | `12210` | loopback HTTP listener port |
| `FILEVIEWER_BIND` | `127.0.0.1` | bind address (loopback only by design) |

## Scope & caveats

- **Any absolute path the awm user can read is viewable** — there is no
  workspace-root restriction, by design. The listener binds `127.0.0.1` only
  (no remote reach), matching awm's loopback-and-unauthenticated posture, but it
  is a broader read surface than the JSON services. An allowed-roots env gate is
  trivial to add later if ever wanted.
- **Single-file only.** No directory listing, and relative `src`/`href` assets
  referenced *inside* an HTML/SVG document won't resolve — a self-contained
  file (the SVG/HTML case) views perfectly; a multi-file site shows only its
  entry document.

## Verify

    awm services list                                        # fileviewer → running
    awm fileviewer status                                   # bind/port/serving/requests
    curl -i "http://127.0.0.1:12210/?path=/tmp/probe.svg"    # 200 + image/svg+xml
    curl -i "http://127.0.0.1:12210/?path=/does/not/exist"   # 404 + not-found page

Then open the SVG/HTML URL in a real browser to confirm it *renders*, not just
transfers.
