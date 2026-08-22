# Installing the `mic` service (remote microphone)

A Python feature service in the `awm.mic` namespace. It gives a host with no
capture device a microphone fed from a phone's browser: the page at `/ui/mic`
captures the phone's mic and streams s16le PCM into a PulseAudio `virtmic`
null-sink whose `.monitor` is the default capture source — so `/voice`,
`arecord`, and the awm `stt` stack record the phone.

The page is an ordinary awm page and the audio is an ordinary awm session
(`kind="stream"`, `transport="direct"`, opened at `/svc/mic/session/stream`).
`getUserMedia` needs a secure context, and [`httpsfront`](../httpsfront/INSTALL.md)
supplies one for every awm page — so mic binds no port and mints no certificate.
It used to do both, which is why it could not start at all on a node holding
only the public half of the fleet CA.

**mic does not own the audio plumbing.** PulseAudio and the `virtmic` null-sink
belong to the [`virtmic`](../virtmic/INSTALL.md) service, which keeps them alive
across daemon restarts. mic is a consumer: it pipes PCM in with `pacat` and calls
`virtmic_ensure` immediately before starting a stream (the gateway guarantees no
start order between services, so the dependency is explicit rather than
temporal). `mic_ensure_sink` still exists as a deprecated alias that forwards to
`virtmic_ensure`.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`) and writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` = the env's absolute interpreter, so the gateway can respawn the
service under systemd's minimal PATH (where `mamba` is not present).

The page is built with the rest of the frontend (`npm run build` in `awm/`); the
built bundle is git-ignored, so a deploy that skips the build ships no page.

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control/session loop) |

## System dependencies (NOT pip-installable)

| Tool | Package | Why |
|---|---|---|
| `pacat` | `pulseaudio-utils` | pipe browser PCM into the `virtmic` sink (the sink itself is provisioned by the `virtmic` service) |

    sudo apt-get install -y pulseaudio pulseaudio-utils

It lives in `/usr/bin`, on the minimal systemd PATH the supervisor uses. `pacat`
also needs `XDG_RUNTIME_DIR` to find the user's PulseAudio socket, which systemd's
environment does not set; mic repairs that for its own subprocess.

## Exposure

The posture inverted in both directions when mic moved onto the hub, and both
halves are worth knowing:

- The **page** used to be an unauthenticated public port. It is now behind
  httpsfront's password like every other awm page.
- The **audio session** used to be reachable only from off-host. It is now
  reachable by anything that can reach the loopback gateway — i.e. by anything
  already running as this user.

A device that does not yet trust the node's CA can install it from the
unauthenticated `/ca.crt` on the edge; the login page links it, and so does the
mic page's footer.

## Always-on gateway

If the mic is permanent infrastructure on this host, the gateway must not idle
out: set `AWM_IDLE_SHUTDOWN=0` in the gateway's `$AWM_WORKSPACE/.awm/env` and
restart `awm.service`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `MIC_SINK` | `virtmic` | PulseAudio null-sink name |

## Verify

    awm services list                       # mic → running
    awm mic status                          # sink, active_streams, audio_path_ok
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7819/ui/mic/
    pactl get-default-source                # virtmic.monitor

Then open `/ui/mic/` — on `http://127.0.0.1:<hub port>` locally (loopback is a
secure context, so this needs no TLS and no login) or on
`https://<host>:$AWM_HTTPS_PORT` from the phone — tap **START**, and confirm the
badge turns purple. Purple means the service reported `ready`, i.e. `pacat` is
actually running; an open socket alone never turns it. Then
`pactl list sink-inputs | grep -A6 awm-mic` should show the live stream, and
`parec -d virtmic.monitor | …` (or Claude Code `/voice`) should hear the phone.
