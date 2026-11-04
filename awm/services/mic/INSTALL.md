# Installing the `mic` service (remote microphone)

A Python feature service in the `awm.mic` namespace. It gives a host with no
capture device a microphone fed from a phone's browser: the page at `/ui/mic`
captures the phone's mic and streams s16le PCM into a PulseAudio `virtmic`
null-sink whose `.monitor` is the default capture source — so `/voice` and
`arecord` record the phone.

The page is an ordinary awm page and the audio is an ordinary awm session
(`kind="stream"`, `transport="direct"`, opened at `/hub/session/mic/stream`).
`getUserMedia` needs a secure context, and [`httpsfront`](../httpsfront/INSTALL.md)
supplies one for every awm page — so mic binds no port and mints no certificate.
It used to do both, which is why it could not start at all on a node holding
only the public half of the fleet CA.

**mic owns the audio plumbing.** It keeps PulseAudio resident and the `virtmic`
null-sink loaded, through config files, a pass at start, a health loop every
`MIC_HEALTH_INTERVAL_S` seconds, and a pass right before each stream starts.
`mic_ensure_sink` runs one pass now. A separate `virtmic` service held this
until the redux folded it into mic (`install.md` R134).

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
| `pulseaudio`, `pactl`, `pacat` | `pulseaudio`, `pulseaudio-utils` | keep the `virtmic` sink alive, and pipe browser PCM into it |
| `systemctl --user` | systemd | restart a socket-activated PulseAudio after a config write |

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

## It writes outside its own directory

mic writes three files in `$HOME`, because PulseAudio and ALSA read their
configuration only from there.

| File | Why |
|---|---|
| `~/.config/pulse/daemon.conf` | `exit-idle-time = -1` stops the daemon exiting about 20 s after the last client leaves |
| `~/.config/pulse/default.pa` | declares the `virtmic` null-sink, so a daemon restart rebuilds it unaided |
| `~/.asoundrc` | routes ALSA's default device through PulseAudio, so `arecord` sees the mic |

A marker fences each write, so a rerun never duplicates a stanza. The markers keep the old `awm virtmic:` text, so hosts that ran the virtmic service see no new stanza. A file that does not exist yet starts with an `.include` of the system copy, because a user-level PulseAudio file replaces the system one.

**CAUTION** Install PulseAudio before mic first runs. The `.include` is written only when the system file exists, and the marker then stops any rewrite. A run on a node without `pulseaudio` leaves a `default.pa` that loads no modules, so `pactl` hangs. Repair it by hand: prepend the two `.include` lines and restart `pulseaudio.service`.

To remove the durability layers, delete the marker-fenced stanzas by hand.

## Always-on gateway

If the mic is permanent infrastructure on this host, the gateway must not idle
out: set `AWM_IDLE_SHUTDOWN=0` in the gateway's `$AWM_WORKSPACE/.awm/env` and
restart `awm.service`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `MIC_SINK` | `virtmic` | PulseAudio null-sink name |
| `MIC_HEALTH_INTERVAL_S` | `20` | sink health loop period |

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
